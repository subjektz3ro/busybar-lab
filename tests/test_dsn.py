"""Host-side logic for the dsn app. No device, no network."""

import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.dsn_app import formatting as dsn_formatting

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import cli as dsn_cli
from apps.dsn_app import feed as dsn_feed
from apps.dsn_app import history as dsn_history
from apps.dsn_app import input as dsn_input
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import missions as dsn_missions
from apps.dsn_app import model as dsn_model
from apps.dsn_app import ranges as dsn_ranges
from apps.dsn_app import selection as dsn_selection
from apps.dsn_app import settings as dsn_settings
from apps.dsn_app import source as dsn_source
from apps.dsn_app.audio import assets as dsn_audio_assets
from apps.dsn_app.audio import narration as dsn_audio_narration
from apps.dsn_app.audio import policy as dsn_audio_policy
from apps.dsn_app.audio import words as dsn_audio_words
from apps.dsn_app.audio import worker as dsn_audio_worker
from apps.dsn_app.device import assets as dsn_device_assets
from apps.dsn_app.device import display as dsn_device_display
from apps.dsn_app.device import scene_policy as dsn_device_scene_policy
from apps.dsn_app.render import carriers as dsn_render_carriers
from apps.dsn_app.render import craft as dsn_render_craft
from apps.dsn_app.render import dish as dsn_render_dish
from apps.dsn_app.render import distance as dsn_render_distance
from apps.dsn_app.render import globe as dsn_render_globe
from apps.dsn_app.render import labels as dsn_render_labels
from apps.dsn_app.render import palette as dsn_render_palette
from apps.dsn_app.render import text as dsn_render_text
from apps.dsn_app.render import timing as dsn_render_timing
from busybar_dev.device import is_refusal as _is_refusal

# A real capture, trimmed: station/dish are FLAT SIBLINGS, ranges and rtlt are
# partly the feed's "-1" sentinel, and housekeeping targets share the shape.
FEED = b"""<dsn>
 <station name="gdscc" friendlyName="Goldstone" timeUTC="1786046106000" timeZoneOffset="-25200000.0"/>
 <dish name="DSS14" azimuthAngle="0" elevationAngle="90" activity="Engineering Upgrades">
  <target name="DSN" id="99" uplegRange="-1" downlegRange="-1" rtlt="-1"/>
 </dish>
 <dish name="DSS25" azimuthAngle="201" elevationAngle="72" activity="Telemetry">
  <upSignal active="true" signalType="data" dataRate="0" frequency="0" band="X" power="18" spacecraft="JNO" spacecraftID="-61"/>
  <downSignal active="true" signalType="data" dataRate="18090" frequency="0" band="X" power="-130" spacecraft="JNO" spacecraftID="-61"/>
  <target name="JNO" id="61" uplegRange="941000000" downlegRange="941000000" rtlt="-1"/>
 </dish>
 <station name="cdscc" friendlyName="Canberra" timeUTC="1786046106000" timeZoneOffset="36000000.0"/>
 <dish name="DSS43" azimuthAngle="10" elevationAngle="25" activity="Telemetry">
  <downSignal active="true" signalType="data" dataRate="160" frequency="0" band="X" power="-155" spacecraft="VGR2" spacecraftID="-32"/>
  <target name="VGR2" id="32" uplegRange="21000000000" downlegRange="21000000000" rtlt="-1"/>
 </dish>
 <dish name="DSS36" azimuthAngle="1" elevationAngle="19" activity="Telemetry">
  <upSignal active="false" signalType="none" dataRate="0" frequency="0" band="X" power="0" spacecraft="MRO" spacecraftID="-74"/>
 </dish>
 <timestamp>1786046106000</timestamp>
</dsn>"""


def links():
    return dsn_source.parse_feed(FEED)


def test_flat_station_dish_siblings_assign_the_right_complex():
    by_dish = {l.dish: l for l in links()}
    assert by_dish["DSS25"].complex_name == "Goldstone"
    assert by_dish["DSS43"].complex_name == "Canberra"


def test_housekeeping_targets_are_not_spacecraft():
    assert "DSN" not in {l.craft for l in links()}


def test_inactive_signals_are_skipped():
    # DSS36's only signal is active="false"
    assert "DSS36" not in {l.dish for l in links()}


def test_negative_naif_id_is_preserved_for_horizons():
    jno = next(l for l in links() if l.craft == "JNO")
    assert jno.naif == -61          # Horizons wants the negative id


def test_uplink_only_link_still_gets_an_id():
    """MRO is often uplink-only. Reading the id from the downSignal alone
    left it without one, so Horizons was never asked and its distance
    showed '?' forever."""
    feed = FEED.replace(b'<dish name="DSS36" azimuthAngle="1" elevationAngle="19" activity="Telemetry">\n  <upSignal active="false"',
                        b'<dish name="DSS36" azimuthAngle="1" elevationAngle="19" activity="Telemetry">\n  <target name="MRO" id="74" uplegRange="-1" downlegRange="-1" rtlt="-1"/>\n  <upSignal active="true"')
    mro = next(l for l in dsn_source.parse_feed(feed) if l.craft == "MRO")
    assert mro.naif == -74, "uplink-only craft must still resolve a NAIF id"
    assert mro.down_bps is None                     # no receive record was published


def test_light_time_from_downleg_range():
    vgr2 = next(l for l in links() if l.craft == "VGR2")
    assert vgr2.light_s == pytest.approx(21e9 / 299792.458, rel=1e-6)
    assert vgr2.light_s / 3600 == pytest.approx(19.5, abs=0.5)   # ~19.5 hours


def test_dead_sentinel_fields_do_not_become_ranges():
    """rtlt is always -1 and some ranges are -1; neither may read as a distance."""
    for link in links():
        assert link.range_km is None or link.range_km > 0


def test_the_complexes_are_named_from_the_feed():
    """The station element groups the dishes under it. Losing the grouping
    puts every dish in the wrong place on the globe, which is centred on the
    complex that is listening."""
    names = {l.complex_name for l in links()}
    assert "Goldstone" in names and "Canberra" in names


def test_crossing_time_keeps_the_real_ratios():
    """The strip plays at a fixed 1/600 of real time, so what you see is the
    true ratio: Voyager really is ~23x further out than Jupiter."""
    juno = dsn_render_timing.crossing_seconds(3138.0)                  # 52 light-minutes
    voyager = dsn_render_timing.crossing_seconds(71382.0)              # 19.8 light-hours
    assert voyager / juno == pytest.approx(71382 / 3138, rel=0.02)
    # Unknown range uses a neutral planning bound only; its RF marks stay still.
    assert dsn_render_timing.crossing_seconds(None) == dsn_limits.LOOP_S


def test_the_inner_solar_system_does_not_collapse_onto_one_number():
    """A flat 2-second floor drew the Moon, the Lagrange points and Mars at
    closest approach at IDENTICAL speed — 1.3 seconds of light time and 190
    seconds of it, indistinguishable — while claiming true ratios."""
    moon = dsn_render_timing.crossing_seconds(1.3)
    lagrange = dsn_render_timing.crossing_seconds(5.0)
    mars_near = dsn_render_timing.crossing_seconds(190.0)
    mars_far = dsn_render_timing.crossing_seconds(1300.0)
    assert moon < lagrange < mars_near < mars_far, \
        f"{moon:.2f} {lagrange:.2f} {mars_near:.2f} {mars_far:.2f}"
    # still quick enough to feel instant, still slow enough to be motion
    assert moon >= dsn_render_timing.CROSS_MIN_S
    assert moon < 1.0 and mars_near < 2.0
    # and the law is continuous where the two branches meet
    knee = dsn_render_timing.CROSS_KNEE_S
    assert dsn_render_timing.crossing_seconds(knee - 0.5) == pytest.approx(
        dsn_render_timing.crossing_seconds(knee), rel=0.02), "discontinuity at the knee"


def test_loop_duration_is_fixed_so_scrolling_is_decoupled():
    """The loop used to be as long as the crossing, so a name on Voyager
    scrolled over two minutes and the same name on Mars flashed past in two
    seconds. Distance must not leak into the text clock."""
    now = datetime.now(timezone.utc)
    rates = set()
    for link in links():
        _, fps, hold = dsn_render_distance.render_frames(link, now)
        rates.add((fps, hold))
    assert len(rates) == 1, f"playback rate varies with the craft: {rates}"


def test_distance_shows_up_as_packets_in_flight():
    """With a fixed loop, distance is spacing: a far craft has many packets
    creeping, a near one has a single packet crossing fast. Either way each
    packet takes the true light-crossing time."""
    track = 44
    near = dsn_render_carriers.packet_spacing(dsn_render_timing.crossing_seconds(960), track)      # Mars
    far = dsn_render_carriers.packet_spacing(dsn_render_timing.crossing_seconds(71382), track)     # Voyager
    assert near > track >= far          # one packet vs a chain of them
    # speed = spacing / LOOP_S must equal track / crossing for both
    for light_s in (960, 3138, 71382):
        cross = dsn_render_timing.crossing_seconds(light_s)
        speed = dsn_render_carriers.packet_spacing(cross, track) / dsn_limits.LOOP_S
        assert speed == pytest.approx(track / cross, rel=0.01)


def test_light_label_reads_as_time():
    assert dsn_render_labels.light_label(5) == "5SEC"
    assert dsn_render_labels.light_label(3138) == "52M"
    assert dsn_render_labels.light_label(71382) == "19.8H"
    assert dsn_render_labels.light_label(None) == "?"


def test_frames_are_panel_sized_and_seamless_count():
    link = next(l for l in links() if l.craft == "VGR2")
    frames, fps, hold = dsn_render_distance.render_frames(link, datetime.now(timezone.utc))
    assert len(frames) == dsn_limits.ANIM_FRAMES
    assert all(f.size == (dsn_limits.W, dsn_limits.H) for f in frames)
    assert 1 <= fps <= 12
    assert 1 <= hold <= 255          # the .anim duration field is a uint8


def test_the_message_travels_toward_earth():
    """Right to left: the label reports the DOWNLINK, so the dot must move
    the way the data actually flows. It shipped backwards once."""
    link = next(l for l in links() if l.craft == "VGR2")
    frames, _, _ = dsn_render_distance.render_frames(link, datetime.now(timezone.utc))

    def pulse_x(frame):
        """The warmest pixel on the track row — the pulse is amber and
        everything else out there is black or blue. Keyed to TRACK_Y so a
        layout change moves the probe with it."""
        row = dsn_render_palette.TRACK_Y
        # Everything right of the globe, to the panel edge — derived from the
        # layout so moving the globe or the craft can't silently narrow it.
        warm = [(frame.getpixel((x, row))[0] - frame.getpixel((x, row))[2], x)
                for x in range(dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 1, dsn_limits.W)]
        best, x = max(warm)
        return x if best > 40 else None

    lit = [pulse_x(frames[i]) for i in (2, len(frames) // 2, len(frames) - 3)]
    assert all(x is not None for x in lit), f"the pulse vanished: {lit}"
    assert lit[0] > lit[1] > lit[2], f"pulse moved away from Earth: {lit}"


def test_every_glyph_we_can_draw_exists_in_the_font():
    """A missing glyph is silently skipped, leaving a gap — '?' did exactly
    that for a craft whose range is unknown."""
    names = {"vgr2": "Voyager 2", "m01o": "Mars Odyssey"}
    samples = [dsn_render_labels.craft_label(l.craft, names) for l in links()]
    samples += [f"{dsn_render_labels.light_label(x)} {dsn_render_labels.rate_label(y)}"
                for x in (None, 5, 3138, 71382) for y in (0, 160, 18090, 2e6)]
    samples += [l.dish.replace("DSS", "") for l in links()]
    for text in samples:
        for ch in text.upper():
            assert ch in dsn_render_text.FONT, f"{ch!r} (in {text!r}) has no glyph"


def test_full_name_is_never_truncated():
    """Long names scroll rather than being clipped to a stub."""
    names = {"vgr2": "Voyager 2", "ace": "Advanced Composition Explorer"}
    assert dsn_render_labels.craft_label("VGR2", names) == "VOYAGER 2"
    assert dsn_render_labels.craft_label("ACE", names) == "ADVANCED COMPOSITION EXPLORER"
    assert dsn_render_labels.craft_label("XYZ", {}) == "XYZ"         # no map, no problem


def test_short_names_do_not_scroll():
    box = 55
    for phase in (0.0, 0.25, 0.5, 0.99):
        assert dsn_render_text.scroll_offset("JUNO", phase, box) == 0


def test_long_names_scroll_one_whole_cycle_per_loop():
    """Seamless by construction: the label travels its own width plus the
    gap across the loop, so the last frame hands back to the first."""
    label = "ADVANCED COMPOSITION EXPLORER"
    box = 55
    cycle = dsn_render_text.text_width(label) + dsn_render_text.SCROLL_GAP_PX
    assert dsn_render_text.scroll_offset(label, 0.0, box) == 0
    assert dsn_render_text.scroll_offset(label, 0.999, box) == pytest.approx(cycle, abs=2)
    offs = [dsn_render_text.scroll_offset(label, i / 40, box) for i in range(40)]
    assert offs == sorted(offs)                        # never jumps backwards


def test_scrolling_label_stays_inside_its_box():
    """The clip window is what keeps a long name off the globe and the
    dish number."""
    from PIL import Image
    img = Image.new("RGB", (dsn_limits.W, dsn_limits.H), (0, 0, 0))
    px = img.load()
    dsn_render_text._text(px, 4, 0, "ADVANCED COMPOSITION EXPLORER", (255, 255, 255),
              clip=(20, 50))
    lit = [x for x in range(dsn_limits.W) for y in range(dsn_limits.H) if px[x, y] != (0, 0, 0)]
    assert lit, "nothing drawn"
    assert min(lit) >= 20 and max(lit) <= 50, f"escaped the box: {min(lit)}-{max(lit)}"


def test_globe_is_round_without_cardinal_spikes():
    """A bare dx^2+dy^2 <= R^2 leaves one pixel poking out at each of the
    four cardinal points, and the disc reads as a diamond."""
    from PIL import Image
    img = Image.new("RGB", (dsn_limits.W, dsn_limits.H), (0, 0, 0))
    dsn_render_globe._globe(img.load(), 0.0, 0.0, 0.0)
    px = img.load()
    top = [x for x in range(dsn_limits.W) if px[x, dsn_render_palette.GLOBE_CY - dsn_render_palette.GLOBE_R] != (0, 0, 0)]
    assert len(top) != 1, "single pixel spike at the top of the globe"
    widest = max(sum(1 for x in range(dsn_limits.W) if px[x, y] != (0, 0, 0))
                 for y in range(dsn_limits.H))
    tallest = max(sum(1 for y in range(dsn_limits.H) if px[x, y] != (0, 0, 0))
                  for x in range(dsn_limits.W))
    assert abs(widest - tallest) <= 1, f"not round: {widest}x{tallest}"


def test_spoken_report_is_plain_ascii():
    """The bar's text is ASCII-only and TTS chokes on typography."""
    for link in links():
        text = dsn_audio_words.spoken(link)
        assert text.isascii(), text
        assert "None" not in text


def test_sweep_regex_matches_our_generations_only():
    assert dsn_device_assets.GENERATION_FILES.match("dsn_12345_7.anim")
    assert not dsn_device_assets.GENERATION_FILES.match("dsn_12345_7.snd"), \
        "scene cleanup must never claim speech assets"
    assert not dsn_device_assets.GENERATION_FILES.match("sky_12345_1.anim")
    assert not dsn_device_assets.GENERATION_FILES.match("notes.txt")


# --- hardware input --------------------------------------------------------
# The real shape: message -> "updates" -> per-update "input" -> the event.
# Reading events off the message itself parses cleanly and NEVER FIRES, which
# is exactly how this app shipped its controls broken the first time.

def wheel(delta):
    return {"input": {"encoder_event": {"delta": delta}}}


def test_encoder_delta_reads_through_the_input_nesting():
    assert dsn_input.encoder_delta(wheel(3)) == 3
    assert dsn_input.encoder_delta(wheel(-2)) == -2
    assert dsn_input.encoder_delta({"input": {}}) == 0
    assert dsn_input.encoder_delta({}) == 0
    # the bug: the event at the top level must NOT be mistaken for a real one
    assert dsn_input.encoder_delta({"encoder_event": {"delta": 4}}) == 0


def test_empty_button_event_is_an_ok_press():
    """proto3 omits zero values, so {} really is OK+PRESS."""
    assert dsn_input.is_ok_press({"input": {"button_event": {}}})
    assert dsn_input.is_ok_press({"input": {"button_event": {"button": "OK", "action": "PRESS"}}})
    assert not dsn_input.is_ok_press({"input": {}})
    assert not dsn_input.is_ok_press({"input": {"button_event": {"action": "RELEASE"}}})
    assert not dsn_input.is_ok_press({"input": {"button_event": {"button": "START"}}})


def test_start_press_is_distinct_from_ok():
    assert dsn_input.is_start_press({"input": {"button_event": {"button": "START"}}})
    assert dsn_input.is_start_press({"input": {"button_event": {"button": 2}}})
    assert not dsn_input.is_start_press({"input": {"button_event": {}}})   # that's OK
    assert not dsn_input.is_start_press({"input": {"button_event":
                                             {"button": "START", "action": "RELEASE"}}})


def test_one_felt_click_moves_one_signal():
    """The bar emits ONE count per detent — verified in skystrip, which has
    always used 1. dsn asked for 4, so a felt click did nothing three times
    out of four and the wheel felt dead."""
    assert dsn_limits.DETENT_COUNTS == 1
    state = dsn_model.State()
    state.links = [object()] * 4
    moved = []
    for raw in (1, 1, 1, -1):
        state.enc_accum += raw
        while abs(state.enc_accum) >= dsn_limits.DETENT_COUNTS:
            step = 1 if state.enc_accum > 0 else -1
            state.enc_accum -= dsn_limits.DETENT_COUNTS * step
            moved.append(step)
    assert moved == [1, 1, 1, -1], "one click should be one step, each way"
    assert state.enc_accum == 0


def test_refusal_detection_uses_the_real_busylib_attribute():
    from busylib import exceptions
    assert _is_refusal(
        exceptions.BusyBarAPIError("Not drawn due to low priority", status_code=409))
    assert not _is_refusal(
        exceptions.BusyBarAPIError("Failed to open file for writing", status_code=508))


# --- narration and the speech cache ---------------------------------------


def _voice_link(**kw):
    base = dict(complex_name="Canberra", dish="DSS43", craft="VGR2",
                elevation=30.0, band="X", down_bps=160.0, up_active=True,
                range_km=2.1e10)
    base.update(kw)
    return dsn_source.Link(**base)


def test_narration_says_antenna_mission_and_light_time():
    text = dsn_audio_words.spoken(_voice_link(), {"vgr2": "Voyager 2"})
    assert "Canberra" in text                      # which complex
    assert "on dish number 43" in text              # identity without invented size
    assert "metre dish" not in text
    assert "Voyager 2" in text
    assert "interstellar space" in text            # what the mission is
    assert "19 hours" in text                      # how long the signal takes
    assert "160 bits per second" in text


def test_acronym_names_are_not_title_cased():
    """NASA's friendlyName is already cased to be read aloud. .title() turned
    'SOHO' into 'Soho' and 'MAVEN' into 'Maven'."""
    assert "SOHO" in dsn_audio_words.spoken(_voice_link(craft="SOHO"), {"soho": "SOHO"})
    assert "Soho" not in dsn_audio_words.spoken(_voice_link(craft="SOHO"), {"soho": "SOHO"})


def test_a_parenthetical_is_stripped_from_the_spoken_name():
    name = dsn_audio_words.spoken_name("DSCO", {"dsco": "Deep Space Climate Observatory (DSCOVR)"})
    assert name == "Deep Space Climate Observatory"


def test_units_are_singular_when_there_is_one_of_them():
    assert dsn_audio_words.light_words(1.28) == "1 second"     # the Moon
    assert dsn_audio_words.light_words(60.0) == "60 seconds"   # under 90s stays in seconds
    assert dsn_audio_words.light_words(3600.0) == "1 hour"
    assert dsn_audio_words.light_words(3660.0) == "1 hour and 1 minute"
    assert dsn_audio_words.light_words(2.1e10 / dsn_source.C_KM_S).startswith("19 hours and")


def test_uplink_only_does_not_say_transmitting_twice():
    text = dsn_audio_words.spoken(_voice_link(down_bps=0.0), {"vgr2": "Voyager 2"})
    assert text.count("transmitting") == 1
    assert "not listening" not in text


def test_the_same_line_always_gets_the_same_filename():
    """The cache is the filename. Identical text must resolve to an identical
    path so a hit needs no upload and survives a restart."""
    a, b = _voice_link(), _voice_link()
    assert dsn_audio_assets.speech_name(dsn_audio_words.spoken(a, {})) == dsn_audio_assets.speech_name(dsn_audio_words.spoken(b, {}))
    other = _voice_link(craft="JNO", dish="DSS25", complex_name="Goldstone")
    assert dsn_audio_assets.speech_name(dsn_audio_words.spoken(other, {})) != dsn_audio_assets.speech_name(dsn_audio_words.spoken(a, {}))
    assert dsn_audio_assets.VOICE_FILES.match(dsn_audio_assets.speech_name("anything"))


def test_a_jittering_bitrate_does_not_rebake_the_line():
    """Rates are rounded coarsely on purpose: exact bps would change the text,
    and so the hash, on nearly every poll — a synth every 10 seconds."""
    a = dsn_audio_words.spoken(_voice_link(down_bps=2_048_000.0), {})
    b = dsn_audio_words.spoken(_voice_link(down_bps=2_050_113.0), {})
    assert dsn_audio_assets.speech_name(a) == dsn_audio_assets.speech_name(b)


def test_speaking_holds_the_rotation_on_the_narrated_link():
    """The bug this fixes: baking costs about as long as the line lasts, so
    without a hold the strip has rotated on before the audio starts."""
    state = dsn_model.State()
    state.links = [_voice_link(), _voice_link(craft="JNO", dish="DSS25")]
    state.feed_timestamp_ms = int(time.time() * 1000)
    state.feed_advanced_at = time.time()
    link = state.links[0]
    bb = _SpeakBar()

    async def scenario():
        state.speech[dsn_audio_assets.speech_name(dsn_audio_words.spoken(link, state.names))] = 0.0
        task = asyncio.create_task(dsn_audio_narration.speak(bb, state, link))
        await asyncio.sleep(0)
        assert state.narration_focus == link.key, "rotation was not frozen"
        assert state.focus is None, "narration stole the user's lock channel"
        assert state.current() is link
        await task
        assert state.narration_focus is None, "the hold was not released"

    asyncio.run(scenario())


def test_speaking_does_not_steal_a_lock_the_user_set():
    state = dsn_model.State()
    state.links = [_voice_link()]
    state.feed_timestamp_ms = int(time.time() * 1000)
    state.feed_advanced_at = time.time()
    link = state.links[0]
    state.focus = link.key                       # the user clicked the wheel
    state.speech[dsn_audio_assets.speech_name(dsn_audio_words.spoken(link, state.names))] = 0.0
    asyncio.run(dsn_audio_narration.speak(_SpeakBar(), state, link))
    assert state.focus == link.key, "narration released the user's own lock"


def test_a_cache_hit_uploads_nothing():
    state = dsn_model.State()
    link = _voice_link()
    text = dsn_audio_words.spoken(link, {})
    state.speech[dsn_audio_assets.speech_name(text)] = 4.0
    bb = _SpeakBar()
    name, seconds = asyncio.run(dsn_audio_assets.ensure_speech(bb, state, text))
    assert bb.uploads == [], "re-uploaded a line the device already has"
    assert (name, seconds) == (dsn_audio_assets.speech_name(text), 4.0)


def test_the_voice_cache_stays_bounded():
    state = dsn_model.State()
    bb = _SpeakBar()
    for i in range(dsn_limits.SPEECH_CACHE_MAX + 4):
        state.speech[f"voice_{i:012x}.snd"] = 1.0
    asyncio.run(dsn_audio_assets.trim_speech_cache(bb, state))
    assert len(state.speech) == dsn_limits.SPEECH_CACHE_MAX
    assert len(bb.removed) == 4


def test_full_cache_protects_every_active_frozen_narration():
    state = dsn_model.State()
    active_text = "Canberra is listening to Voyager 2."
    active_name = dsn_audio_assets.speech_name(active_text)
    state.narration_texts["DSS43/VGR2"] = active_text
    state.speech[active_name] = 1.0                 # deliberately oldest
    for i in range(dsn_limits.SPEECH_CACHE_MAX):
        state.speech[f"inactive_{i:03d}.snd"] = 1.0
    bb = _SpeakBar()

    asyncio.run(dsn_audio_assets.trim_speech_cache(bb, state))

    assert active_name in state.speech
    assert len(state.speech) == dsn_limits.SPEECH_CACHE_MAX
    assert all(active_name not in path for path in bb.removed)


def test_failed_voice_asset_removal_keeps_the_deterministic_mapping():
    state = dsn_model.State()
    for i in range(dsn_limits.SPEECH_CACHE_MAX + 1):
        state.speech[f"cached_{i:03d}.snd"] = 1.0

    class BrokenStorage:
        async def storage_remove(self, path):
            raise OSError("device disconnected")

    asyncio.run(dsn_audio_assets.trim_speech_cache(BrokenStorage(), state))
    assert len(state.speech) == dsn_limits.SPEECH_CACHE_MAX + 1


def test_a_line_in_constant_use_is_never_evicted(monkeypatch):
    """Eviction was by insertion order, which is not use order. A spacecraft
    whose data rate churns mints a fresh line every pass, so the line you press
    START on most often was evicted while the noise stayed — and re-baking it
    costs about as long as the line lasts."""
    synthesised = []

    def fake_synth(text, voice):
        synthesised.append(text)
        return b"\x00\x00" * 100

    monkeypatch.setattr(dsn_audio_worker, "synth_snd", fake_synth)
    monkeypatch.setattr(dsn_audio_worker, "isolate_tts_process", lambda: False)
    state, bb = dsn_model.State(), _SpeakBar()
    favourite = "Canberra is listening to Voyager 2."

    async def scenario():
        await dsn_audio_assets.ensure_speech(bb, state, favourite)
        for i in range(dsn_limits.SPEECH_CACHE_MAX * 2):
            await dsn_audio_assets.ensure_speech(bb, state, f"Madrid is listening at {i} kilobits.")
            await dsn_audio_assets.ensure_speech(bb, state, favourite)   # ...and used again

    asyncio.run(scenario())
    assert synthesised.count(favourite) == 1, "re-baked a line that was in constant use"
    assert dsn_audio_assets.speech_name(favourite) in state.speech
    assert len(state.speech) == dsn_limits.SPEECH_CACHE_MAX


def test_one_rotation_warms_the_cache_for_good(monkeypatch):
    """Measured on the Pi: 13 distinct dish->craft pairs in the rotation
    against 10 cache slots, so every lap evicted lines the next lap needed and
    re-baked them. Bakes there ran 17-56s wall for 13-39s of audio (~1.35x
    realtime), and pressing START on an evicted line waited out the whole
    synth. The cache must hold a full rotation or it converges on nothing."""
    synthesised = []

    def fake_synth(text, voice):
        synthesised.append(text)
        return b"\x00\x00" * 100

    monkeypatch.setattr(dsn_audio_worker, "synth_snd", fake_synth)
    monkeypatch.setattr(dsn_audio_worker, "isolate_tts_process", lambda: False)
    state, bb = dsn_model.State(), _SpeakBar()
    rotation = [f"Canberra is listening to spacecraft number {i}." for i in range(13)]

    async def lap():
        for text in rotation:
            await dsn_audio_assets.ensure_speech(bb, state, text)

    asyncio.run(lap())
    assert len(synthesised) == len(rotation), "first lap should bake each line once"
    asyncio.run(lap())
    assert len(synthesised) == len(rotation), (
        "second lap re-synthesised lines the first lap had already baked: "
        f"{len(synthesised) - len(rotation)} evicted from a {dsn_limits.SPEECH_CACHE_MAX}-slot cache")


class _SpeakBar:
    """Enough of the device to exercise speak() without hardware."""

    def __init__(self):
        self.uploads, self.played, self.removed = [], [], []

    async def assets_upload(self, app, name, blob):
        self.uploads.append(name)

    async def audio_play(self, application_name=None, path=None):
        self.played.append(path)

    async def storage_remove(self, path):
        self.removed.append(path)


def test_non_linux_synthesis_runs_on_a_daemon_thread(monkeypatch):
    """asyncio.run() joins the default executor at exit, so a 20-second bake
    in flight made the app ignore SIGTERM long enough to be SIGKILLed — and a
    SIGKILL skips the handler that clears the panel."""
    seen = {}

    def fake_synth(text, voice):
        seen["daemon"] = threading.current_thread().daemon
        seen["main"] = threading.current_thread() is threading.main_thread()
        return b"\x00\x00" * 100

    monkeypatch.setattr(dsn_audio_worker, "synth_snd", fake_synth)
    monkeypatch.setattr(dsn_audio_worker, "isolate_tts_process", lambda: False)
    pcm = asyncio.run(dsn_audio_worker.synth_off_loop("hello"))
    assert pcm == b"\x00\x00" * 100
    assert seen["daemon"] is True, "a non-daemon thread blocks interpreter exit"
    assert seen["main"] is False


def test_a_failing_synth_propagates_rather_than_hanging(monkeypatch):
    def boom(text, voice):
        raise RuntimeError("no voice bank")

    monkeypatch.setattr(dsn_audio_worker, "synth_snd", boom)
    monkeypatch.setattr(dsn_audio_worker, "isolate_tts_process", lambda: False)
    with pytest.raises(RuntimeError):
        asyncio.run(dsn_audio_worker.synth_off_loop("hello"))

    # ensure_speech turns that into a None, not a crashed task
    state = dsn_model.State()
    assert asyncio.run(dsn_audio_assets.ensure_speech(_SpeakBar(), state, "hello")) is None
    assert state.speech == {}


def test_linux_synthesis_uses_a_disposable_worker(monkeypatch):
    seen = {}

    class Process:
        returncode = None

        def __init__(self, output):
            self.output = output

        async def communicate(self, data):
            seen["input"] = data
            self.output.write_bytes(b"\x01\x00" * 100)
            self.returncode = 0
            return b"ignored library chatter", b"one harmless warning"

        async def wait(self):
            return self.returncode

    async def create(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        output = Path(args[args.index("--output") + 1])
        seen["output"] = output
        return Process(output)

    def forbidden(*args):
        raise AssertionError("Kokoro loaded in the long-lived parent")

    monkeypatch.setattr(dsn_audio_worker, "isolate_tts_process", lambda: True)
    monkeypatch.setattr(dsn_audio_worker, "synth_snd", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    pcm = asyncio.run(dsn_audio_worker.synth_off_loop("hello from Earth"))

    assert pcm == b"\x01\x00" * 100
    assert seen["input"] == b"hello from Earth"
    assert seen["args"][:3] == (
        sys.executable, "-m", "busybar_dev.tts_worker")
    assert seen["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert not seen["output"].exists(), "private PCM file leaked after adoption"


def test_cancelling_an_isolated_bake_terminates_its_process(monkeypatch):
    seen = {}

    class Process:
        returncode = None

        async def communicate(self, data):
            seen["started"].set()
            await asyncio.Event().wait()

        def terminate(self):
            seen["terminated"] = True
            self.returncode = -15

        def kill(self):
            seen["killed"] = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def create(*args, **kwargs):
        seen["output"] = Path(args[args.index("--output") + 1])
        return Process()

    monkeypatch.setattr(dsn_audio_worker, "isolate_tts_process", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def scenario():
        seen["started"] = asyncio.Event()
        task = asyncio.create_task(dsn_audio_worker.synth_off_loop("cancel me"))
        await seen["started"].wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert seen["terminated"] is True
    assert "killed" not in seen
    assert not seen["output"].exists(), "cancelled worker left private PCM behind"


def test_isolated_worker_failure_reports_stderr_and_cleans_up(monkeypatch):
    seen = {}

    class Process:
        returncode = None

        async def communicate(self, data):
            self.returncode = 7
            return b"", b"model missing: \xff"

        async def wait(self):
            return self.returncode

    async def create(*args, **kwargs):
        seen["output"] = Path(args[args.index("--output") + 1])
        return Process()

    monkeypatch.setattr(dsn_audio_worker, "isolate_tts_process", lambda: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(RuntimeError, match=r"exited 7: model missing"):
        asyncio.run(dsn_audio_worker.synth_off_loop("hello"))
    assert not seen["output"].exists(), "failed worker left private PCM behind"


def test_one_shot_worker_writes_only_pcm_to_its_private_file(
        monkeypatch, tmp_path):
    from busybar_dev import tts_worker

    output = tmp_path / "line.snd"
    monkeypatch.setattr(
        tts_worker, "synth_snd",
        lambda text, voice: f"{voice}:{text}".encode())

    tts_worker.render("hello", "af_nova", output)
    assert output.read_bytes() == b"af_nova:hello"


def test_an_explicit_voice_beats_the_environment(monkeypatch):
    """synth_snd read SKYSTRIP_VOICE straight from the environment and threw
    its own argument away, so dsn asked for af_nova and got am_michael."""
    from busybar_dev import tts

    seen = {}
    monkeypatch.setenv("SKYSTRIP_VOICE", "am_michael")
    monkeypatch.setattr(tts, "_kokoro_paths", lambda: ("model", "bank"))
    monkeypatch.setattr(
        tts,
        "tts_engine_status",
        lambda: ("kokoro", "test Kokoro engine"),
    )
    def fake_kokoro(text, voice):
        seen["voice"] = voice
        return b"\x00\x10" * 50, 44100

    monkeypatch.setattr(tts, "_kokoro_synth", fake_kokoro)
    tts.synth_snd("hello there", "af_nova")
    assert seen["voice"] == "af_nova"

    seen.clear()                       # no argument: the env is the default
    tts.synth_snd("hello there")
    assert seen["voice"] == "am_michael"

    # A gitignored host config survives upgrades.  A now-unsupported voice id
    # must migrate to the tracked Kokoro default when that model is resident.
    seen.clear()
    monkeypatch.setenv("SKYSTRIP_VOICE", "retired-neural-voice")
    tts.synth_snd("hello there")
    assert seen["voice"] == tts.DEFAULT_KOKORO_VOICE == "am_michael"


def test_mac_say_fallback_preserves_an_explicit_system_voice():
    from busybar_dev import tts

    assert tts._say_voice("Samantha") == "Samantha"
    assert tts._say_voice("am_michael") == tts.DEFAULT_VOICE


def test_kokoro_paths_reject_partial_model_downloads(monkeypatch, tmp_path):
    from busybar_dev import tts

    model = tmp_path / "kokoro-v1.0.onnx"
    bank = tmp_path / "voices-v1.0.bin"
    model.write_bytes(b"partial")
    bank.write_bytes(b"partial")
    monkeypatch.setenv("SKYSTRIP_VOICE_DIR", str(tmp_path))

    assert tts._kokoro_paths() is None

    # Sparse files exercise the exact file-size gate without allocating
    # hundreds of MiB in the test checkout.
    with model.open("wb") as output:
        output.truncate(tts.KOKORO_MODEL_SIZE)
    with bank.open("wb") as output:
        output.truncate(tts.KOKORO_BANK_SIZE)
    assert tts._kokoro_paths() == (model, bank)


def test_blank_kokoro_directory_uses_the_checkout_default(monkeypatch, tmp_path):
    """An explicit blank is a config reset, not ``Path("")`` / the cwd."""
    from busybar_dev import tts

    default_dir = tmp_path / "checkout-voices"
    cwd = tmp_path / "working-directory"
    default_dir.mkdir()
    cwd.mkdir()
    for directory in (default_dir, cwd):
        with (directory / "kokoro-v1.0.onnx").open("wb") as output:
            output.truncate(tts.KOKORO_MODEL_SIZE)
        with (directory / "voices-v1.0.bin").open("wb") as output:
            output.truncate(tts.KOKORO_BANK_SIZE)

    monkeypatch.setattr(tts, "DEFAULT_KOKORO_DIR", default_dir)
    monkeypatch.setenv("SKYSTRIP_VOICE_DIR", "")
    monkeypatch.chdir(cwd)

    assert tts._kokoro_paths() == (
        default_dir / "kokoro-v1.0.onnx",
        default_dir / "voices-v1.0.bin",
    )


def test_configured_kokoro_directory_is_absolute_or_checkout_relative(tmp_path):
    from busybar_dev import tts

    env = tmp_path / ".env"
    env.write_text("SKYSTRIP_VOICE_DIR=host-models/kokoro\n")
    assert tts.configured_kokoro_dir(env) == (
        tts.DEFAULT_KOKORO_DIR.parent / "host-models" / "kokoro"
    ).resolve()

    absolute = tmp_path / "absolute-model-bank"
    env.write_text(f'SKYSTRIP_VOICE_DIR="{absolute}"\n')
    assert tts.configured_kokoro_dir(env) == absolute.resolve()


def test_runtime_kokoro_directory_is_checkout_relative(monkeypatch, tmp_path):
    from busybar_dev import tts

    checkout = tmp_path / "checkout"
    model_dir = checkout / "host-models"
    elsewhere = tmp_path / "unrelated-working-directory"
    model_dir.mkdir(parents=True)
    elsewhere.mkdir()
    with (model_dir / "kokoro-v1.0.onnx").open("wb") as output:
        output.truncate(tts.KOKORO_MODEL_SIZE)
    with (model_dir / "voices-v1.0.bin").open("wb") as output:
        output.truncate(tts.KOKORO_BANK_SIZE)

    monkeypatch.setattr(tts, "DEFAULT_KOKORO_DIR", checkout / "voices")
    monkeypatch.setenv("SKYSTRIP_VOICE_DIR", "host-models")
    monkeypatch.chdir(elsewhere)

    assert tts._kokoro_paths() == (
        model_dir / "kokoro-v1.0.onnx",
        model_dir / "voices-v1.0.bin",
    )


def test_retained_models_with_damaged_environment_use_system_fallback(
    monkeypatch,
):
    """Emergency fallback survives a damaged environment after installation."""
    from busybar_dev import tts

    monkeypatch.setattr(tts, "_kokoro_paths", lambda: ("model", "bank"))
    monkeypatch.setattr(tts, "_kokoro_importable", lambda: False)
    monkeypatch.setattr(
        tts.shutil,
        "which",
        lambda command: "/usr/bin/espeak-ng" if command == "espeak-ng" else None,
    )
    monkeypatch.setattr(
        tts,
        "_kokoro_synth",
        lambda *_args: pytest.fail("retained models selected unavailable Kokoro"),
    )

    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        output = Path(args[args.index("-w") + 1])
        with tts.wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44_100)
            wav.writeframes(b"\0\0" * 20)

    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    engine, summary = tts.tts_engine_status()
    rendered = tts.synth_snd("fallback works")

    assert engine == "espeak-ng"
    assert "fallback selected" in summary
    assert "Retained Kokoro models were ignored" in summary
    assert calls and calls[0][0] == "espeak-ng"
    assert rendered == b"\0\0" * 20


@pytest.mark.parametrize(
    "pcm",
    (
        b"\0\0" * 4_410,
        (1).to_bytes(2, "little", signed=True) * 4_410,
        (
            (1).to_bytes(2, "little", signed=True)
            + (-1).to_bytes(2, "little", signed=True)
        ) * 2_205,
    ),
)
def test_kokoro_production_smoke_rejects_silent_or_flat_pcm(monkeypatch, pcm):
    from busybar_dev import tts

    monkeypatch.setattr(
        tts,
        "tts_engine_status",
        lambda: ("kokoro", "test Kokoro engine"),
    )
    monkeypatch.setattr(tts, "_kokoro_synth", lambda *_args: (pcm, 44_100))
    monkeypatch.setattr(tts, "_normalize", lambda frames, *_args: frames)

    with pytest.raises(RuntimeError, match="silent or flat raw samples"):
        tts.verify_kokoro_synthesis()


def test_kokoro_production_smoke_accepts_real_pcm(monkeypatch, tmp_path):
    from busybar_dev import tts

    inherited_dir = tmp_path / "inherited-shell-models"
    configured_dir = tmp_path / "service-models"
    monkeypatch.setenv("SKYSTRIP_VOICE_DIR", str(inherited_dir))

    def fake_status():
        assert os.environ["SKYSTRIP_VOICE_DIR"] == str(configured_dir)
        return "kokoro", "test Kokoro engine"

    monkeypatch.setattr(tts, "tts_engine_status", fake_status)
    pcm = (
        (1_500).to_bytes(2, "little", signed=True)
        + (-1_500).to_bytes(2, "little", signed=True)
    ) * 2_205
    monkeypatch.setattr(tts, "_kokoro_synth", lambda *_args: (pcm, 44_100))
    monkeypatch.setattr(tts, "_normalize", lambda frames, *_args: frames)

    summary = tts.verify_kokoro_synthesis(configured_dir)

    assert "real synthesis" in summary
    assert "raw peak 1,500" in summary


def test_kokoro_production_smoke_cannot_accept_fallback_audio(monkeypatch):
    import busybar_dev
    from busybar_dev import tts

    invalid_dir = "/invalid/operator/model-directory"
    monkeypatch.delenv("SKYSTRIP_VOICE_DIR", raising=False)

    def fake_load_env():
        monkeypatch.setenv("SKYSTRIP_VOICE_DIR", invalid_dir)
        return {"SKYSTRIP_VOICE_DIR": invalid_dir}

    monkeypatch.setattr(busybar_dev, "load_env", fake_load_env)
    monkeypatch.setattr(
        tts,
        "_kokoro_paths",
        lambda: None if os.environ.get("SKYSTRIP_VOICE_DIR") == invalid_dir
        else ("model", "bank"),
    )
    monkeypatch.setattr(tts, "_kokoro_importable", lambda: True)
    monkeypatch.setattr(
        tts.shutil,
        "which",
        lambda command: "/usr/bin/espeak-ng" if command == "espeak-ng" else None,
    )
    monkeypatch.setattr(
        tts,
        "_kokoro_synth",
        lambda *_args: pytest.fail("Kokoro ran despite the invalid model path"),
    )

    with pytest.raises(RuntimeError, match="required Kokoro engine is unavailable"):
        tts.verify_kokoro_synthesis()


def test_the_voice_is_part_of_the_cache_key():
    """Filenames are the cache. If only the text is hashed, switching voices
    keeps serving lines the previous narrator recorded."""
    text = "Canberra is listening to Voyager 2."
    assert dsn_audio_assets.speech_name(text, "af_nova") != dsn_audio_assets.speech_name(text, "am_michael")
    assert dsn_audio_assets.speech_name(text, "af_nova") == dsn_audio_assets.speech_name(text, "af_nova")
    assert dsn_audio_assets.speech_name(text) == dsn_audio_assets.speech_name(text, dsn_settings.VOICE)


def test_the_filename_says_which_voice_recorded_it():
    text = "Canberra is listening to Voyager 2."
    assert dsn_audio_assets.speech_name(text, "af_nova").startswith("v2_afnova_")
    assert dsn_audio_assets.speech_name(text, "am_michael").startswith("v2_ammichael_")
    for name in (dsn_audio_assets.speech_name(text, "af_nova"), dsn_audio_assets.speech_name(text, "Karen")):
        assert dsn_audio_assets.VOICE_FILES.match(name), name


def test_long_voice_names_are_distinct_and_fit_the_firmware_filename_buffer():
    """Configured voice identifiers can exceed the firmware's C buffer."""
    text = "Canberra is listening to Voyager 2."
    voices = ("af_narrator_with_a_deliberately_long_name",
              "af_narrator_with_a_deliberately_long_tone")
    names = [dsn_audio_assets.speech_name(text, voice) for voice in voices]

    assert names[0] != names[1]
    assert all(dsn_audio_assets.VOICE_FILES.fullmatch(name) for name in names)
    assert all(
        len(name.encode("ascii")) <= dsn_limits.DEVICE_ASSET_FILENAME_MAX
        for name in names
    )


def test_startup_reclaims_lines_recorded_by_another_voice():
    """Switching DSN_VOICE in the UI would otherwise strand a full cache of
    ~1.8 MB files on flash, unreachable and never swept."""
    mine = dsn_audio_assets.speech_name("a line", dsn_settings.VOICE)
    theirs = dsn_audio_assets.speech_name("a line", "some_other_voice")
    legacy = "voice_afnova_0f76f95857.snd"     # the previous generation
    bb = _ListingBar([mine, theirs, legacy, "dsn_12345_1.anim"])
    state = dsn_model.State()
    asyncio.run(dsn_audio_assets.load_speech_cache(bb, state))
    assert mine in state.speech
    assert theirs not in state.speech
    # A name we cannot recognise as ours is reclaimable too, or a change of
    # scheme strands the whole cache where no sweep can ever see it again.
    assert sorted(bb.removed) == sorted(
        [f"/ext/user_assets/dsn/{theirs}", f"/ext/user_assets/dsn/{legacy}"])
    assert not any("dsn_12345" in r for r in bb.removed), "ate a scene file"


class _ListingBar(_SpeakBar):
    def __init__(self, names):
        super().__init__()
        self._names = names

    async def storage_list(self, path):
        class Entry:
            def __init__(self, name):
                self.type, self.name, self.size = "file", name, 1_800_000

        class Listing:
            pass

        out = Listing()
        out.list = [Entry(n) for n in self._names]
        return out


def test_dry_run_renders_every_link_without_a_device():
    """--dry-run crashed with a NameError for two commits. It is the first
    command the README tells you to run, and nothing covered it."""
    links = [_voice_link(),
             _voice_link(craft="MRO", dish="DSS36", complex_name="Canberra",
                         range_km=2.95e8, down_bps=2e6),
             _voice_link(craft="LUCY", dish="DSS24", complex_name="Goldstone",
                         range_km=None, down_bps=0.0)]
    out = dsn_cli.describe_links(links, {"vgr2": "Voyager 2",
                                     "mro": "Mars Reconnaissance Orbiter"})
    assert out[0] == "3 active link(s)"
    assert len(out) == 1 + 2 * len(links)
    blob = "\n".join(out)
    assert "Mars Reconnaissance Orbiter" in blob, "narration preview lost the names"
    assert "?" in blob, "a link with no distance should preview as unknown"


def test_each_label_sits_at_the_end_it_describes():
    """The antenna belongs beside the globe it stands on, the spacecraft's
    name beside the spacecraft. They used to be swapped."""
    link = _voice_link(dish="DSS43", craft="VGR2")
    frames, _, _ = dsn_render_distance.render_frames(link, datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
                                     {"vgr2": "VOYAGER 2"})
    px = frames[0].load()

    def lit_columns(y0, y1):
        return {x for x in range(dsn_limits.W) for y in range(y0, y1)
                if px[x, y] != (0, 0, 0)}

    top = lit_columns(0, 5)
    globe_edge = dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R
    dish_ink = {x for x in top if x <= globe_edge + 12}
    name_ink = {x for x in top if x > globe_edge + 14}
    assert dish_ink, "no antenna label beside the globe"
    assert name_ink, "no spacecraft name at the spacecraft end"
    assert max(name_ink) > 50, "the name should run toward the craft"


def test_the_two_halves_of_a_row_are_divided():
    """'43' and 'VOYAGER 2' abutting read as '43VOYAGER 2'."""
    link = _voice_link(dish="DSS43", craft="VGR2")
    frames, _, _ = dsn_render_distance.render_frames(link, datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
                                     {"vgr2": "VOYAGER 2"})
    px = frames[0].load()
    rule = [x for x in range(dsn_limits.W)
            if all(px[x, y] == (40, 55, 75) for y in range(0, 5))]
    assert len(rule) == 1, f"expected exactly one divider column, got {rule}"
    assert dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R < rule[0] < 40


def test_dish_size_comes_from_nasa_not_a_hardcoded_list():
    """NASA adds antennas -- DSS-23 arrived in 2022. The published type wins;
    an absent config stays unknown rather than inventing a diameter."""
    live = {"DSS99": "70M", "DSS43": "34MHEF"}
    assert dsn_formatting.dish_metres("DSS99", live) == "70"
    assert dsn_formatting.dish_metres("DSS43", live) == "34"      # live type overrides it
    assert dsn_formatting.dish_metres("DSS43", {}) is None
    assert dsn_formatting.dish_metres("DSS25", None) is None
    text = dsn_audio_words.spoken(_voice_link(dish="DSS43"), {}, {"DSS43": "70M"})
    assert "70 metre dish, number 43" in text


def test_the_picker_names_the_signal_and_its_place_in_the_list():
    state = dsn_model.State()
    state.links = [_voice_link(craft="JNO"), _voice_link(craft="SOHO"),
                   _voice_link(craft="VGR2")]
    state.cursor = 1
    assert dsn_device_display.picker_label(state) == "SOHO 2/3"
    state.cursor = 2
    assert dsn_device_display.picker_label(state) == "VGR2 3/3"
    state.cursor = 5                                  # wraps, never indexes off
    assert dsn_device_display.picker_label(state) == "VGR2 3/3"
    assert dsn_device_display.picker_label(dsn_model.State()) == "NO SIGNAL"


def test_the_picker_fits_the_device_font_budget():
    """~12 characters at font='condensed'. The full name would overflow, which
    is why this shows the feed's short code."""
    state = dsn_model.State()
    state.links = [_voice_link(craft=c) for c in
                   ("M01O", "PSYC", "NHPC", "VGR2", "MRO", "SOHO", "LUCY",
                    "EURC", "JWST", "CHDR", "DSCO", "WIND")]
    for i in range(len(state.links)):
        state.cursor = i
        assert len(dsn_device_display.picker_label(state)) <= 12, dsn_device_display.picker_label(state)


def test_the_picker_keeps_identical_geometry_on_every_redraw():
    """FIRMWARE LAW: geometry is immutable once an element is drawn. Only the
    text and the timeout may change, or the pop-up silently freezes."""
    a = dsn_device_display._picker_payload("JNO 1/5", timeout=3)
    b = dsn_device_display._picker_payload("SOHO 2/5", timeout=1)
    for ea, eb in zip(a.elements, b.elements):
        assert ea.id == eb.id
        for attr in ("x", "y", "width", "height", "align", "font", "color"):
            if hasattr(ea, attr):
                assert getattr(ea, attr) == getattr(eb, attr), attr
    assert a.elements[0].fill_colors == ["#000000FF"], "backdrop must be opaque"


def test_native_readout_scrolls_complete_long_labels_instead_of_slicing():
    label = "ENGINEERING UPGRADES"
    payload = dsn_device_display._picker_payload(label, timeout=2)

    assert payload.elements[1].text == label
    assert payload.elements[1].scroll_rate == 1400


def test_rotation_does_not_move_the_cursor_while_you_are_picking():
    """Auto-rotate firing mid-pick would move the selection out from under
    the wheel between two detents."""
    state = dsn_model.State()
    state.links = [_voice_link(craft="JNO"), _voice_link(craft="SOHO")]
    state.picking = True
    before = state.cursor

    async def one_tick():
        task = asyncio.create_task(dsn_selection.rotate(state))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(one_tick())
    assert state.cursor == before


# --- real time -------------------------------------------------------------


def test_progress_runs_from_the_craft_to_earth():
    light, lock = 1000.0, 1_780_000_000.0
    # the head starts AT the craft the moment you lock on, not part-way across
    assert dsn_render_timing.realtime_progress(light, lock, lock) == pytest.approx(0.0)
    assert dsn_render_timing.realtime_progress(light, lock + 250, lock) == pytest.approx(0.25)
    assert dsn_render_timing.realtime_progress(light, lock + 999, lock) == pytest.approx(0.999)
    # and it STOPS at arrival — the watch ends there, it does not restart
    assert dsn_render_timing.realtime_progress(light, lock + 1001, lock) == 1.0
    assert dsn_render_timing.realtime_progress(0, 123.0, 0.0) == 0.0
    assert dsn_render_timing.realtime_progress(None, 123.0, 0.0) == 0.0


def test_progress_advances_at_the_true_speed_of_light():
    light = 2.1e10 / dsn_source.C_KM_S
    track = dsn_limits.TRACK1 - dsn_limits.TRACK0
    per_pixel = light / track                          # ~27 min on Voyager
    lock = 1_780_000_000.0
    ts = lock + light * 0.25                           # mid-flight, clear of
                                                       # the arrival wrap
    moved = (dsn_render_timing.realtime_progress(light, ts + per_pixel, lock)
             - dsn_render_timing.realtime_progress(light, ts, lock)) * track
    assert moved == pytest.approx(1.0, abs=0.01), "one pixel per per-pixel time"
    assert 1500 < per_pixel < 1800


def test_redraw_cadence_tracks_how_fast_the_chain_actually_moves():
    mars = 960.0                                     # ~16 min light time
    voyager = 2.1e10 / dsn_source.C_KM_S
    assert 15 < dsn_render_timing.realtime_redraw_s(mars) < 30, "Mars should visibly creep"
    assert dsn_render_timing.realtime_redraw_s(voyager) == 300.0, "clamped, not once a day"
    # a near craft is animated by the device itself; no fast host redraws
    assert dsn_render_timing.realtime_redraw_s(1.3) == float(dsn_limits.REDRAW_S)
    assert dsn_render_timing.realtime_redraw_s(None) == float(dsn_limits.REDRAW_S)


def test_locking_switches_the_chain_off_the_browsing_speed():
    """Compressed browsing puts ~14 evenly spaced marks on Voyager. Real time
    must place them from the clock instead."""
    link = _voice_link(range_km=2.1e10)
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    browse, _, _ = dsn_render_distance.render_frames(link, when, {})
    live, _, _ = dsn_render_distance.render_frames(link, when, {}, realtime_since=when.timestamp())

    def track_marks(frame):
        px = frame.load()
        return {x for x in range(dsn_limits.TRACK0, dsn_limits.TRACK1 + 1)
                if px[x, dsn_render_palette.TRACK_Y] == dsn_render_palette.PULSE}

    # browsing animates: the marks move between frames. Locked, they do not.
    assert track_marks(browse[0]) != track_marks(browse[20])
    assert track_marks(live[0]) == track_marks(live[20]), \
        "a 19.8h crossing cannot move within an 8s loop"


def test_a_locked_scene_says_it_is_live():
    """The chain may not move a pixel for half an hour, so something has to
    show the strip is alive rather than stuck."""
    link = _voice_link(range_km=2.1e10)
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    live, _, _ = dsn_render_distance.render_frames(link, when, {}, realtime_since=when.timestamp())
    browse, _, _ = dsn_render_distance.render_frames(link, when, {})
    on = [f for f in live if f.load()[0, 0] != (0, 0, 0)]
    off = [f for f in live if f.load()[0, 0] == (0, 0, 0)]
    assert on and off, "the live marker should blink, not sit on"
    assert all(f.load()[0, 0] == (0, 0, 0) for f in browse), \
        "browsing must not show a live marker"


def test_the_two_modes_do_not_read_the_same():
    link = _voice_link(range_km=2.1e10)
    when = datetime(2026, 8, 6, 18, 0, tzinfo=timezone.utc)
    browse, _, _ = dsn_render_distance.render_frames(link, when, {})
    live, _, _ = dsn_render_distance.render_frames(link, when, {}, realtime_since=when.timestamp())

    def bottom_row(frame):
        px = frame.load()
        return {(x, y) for x in range(dsn_limits.W) for y in range(dsn_limits.H - 5, dsn_limits.H)
                if px[x, y] != (0, 0, 0)}

    assert bottom_row(browse[0]) != bottom_row(live[0]), \
        "locked and browsing must be distinguishable at a glance"


# --- the font --------------------------------------------------------------


def test_no_glyph_is_a_solid_block():
    """M and W were 14 of 20 cells with two ADJACENT fully-filled rows, so
    they rendered as blobs rather than letters — the failure is structural,
    not a matter of taste. A single filled row is a legitimate crossbar (E,
    T, Z all have one); two touching ones are a filled rectangle."""
    for ch, glyph in dsn_render_text.FONT.items():
        width = len(glyph[0])
        full = [i for i, row in enumerate(glyph) if row == "1" * width]
        adjacent = [i for i in full if i + 1 in full]
        assert not adjacent, f"{ch!r} has filled rows {adjacent} and {adjacent[0]+1}"
        ink = sum(row.count("1") for row in glyph)
        assert ink / (width * len(glyph)) < 0.70, f"{ch!r} is {ink} cells of ink"


def test_no_two_glyphs_are_confusable():
    """'0' was byte-identical to 'O' and '5' to 'S' at 3x5, and ACE read as
    55 on the panel. Glyphs of different widths cannot collide."""
    seen = {}
    for ch, glyph in dsn_render_text.FONT.items():
        if ch == " ":
            continue
        assert glyph not in seen, f"{ch!r} is identical to {seen.get(glyph)!r}"
        seen[glyph] = ch
    items = [(c, g) for c, g in dsn_render_text.FONT.items() if c != " "]
    for i, (a, ga) in enumerate(items):
        for b, gb in items[i + 1:]:
            if len(ga[0]) != len(gb[0]):
                continue                       # different widths, never confusable
            diff = sum(1 for ra, rb in zip(ga, gb)
                       for ca, cb in zip(ra, rb) if ca != cb)
            assert diff > 1, f"{a!r} and {b!r} differ by only {diff} pixel(s)"


def test_the_font_is_proportional_and_measured_correctly():
    """Width is per glyph now: M and W are 5 wide, I and 1 are 3. A fixed
    stride would overlap the wide ones and leave a hole after the narrow."""
    assert dsn_render_text.glyph_width("M") == 5 and dsn_render_text.glyph_width("W") == 5
    assert dsn_render_text.glyph_width("I") == 3 and dsn_render_text.glyph_width("1") == 3
    assert dsn_render_text.glyph_width("A") == 4
    assert all(len(g) == 5 for g in dsn_render_text.FONT.values()), "all glyphs are 5 tall"
    for ch, glyph in dsn_render_text.FONT.items():
        assert len({len(r) for r in glyph}) == 1, f"{ch!r} has ragged rows"


def test_measured_width_matches_what_is_actually_drawn():
    """text_width drives every layout decision — the scroll box, the row fit.
    If it disagrees with the renderer, labels overlap or scroll when they fit."""
    from PIL import Image
    for text in ("MW", "III", "VOYAGER 2", "18:43", "246KBPS", "M1I W", ""):
        img = Image.new("RGB", (dsn_limits.W, dsn_limits.H), (0, 0, 0))
        dsn_render_text._text(img.load(), 0, 0, text, (255, 255, 255))
        px = img.load()
        lit = [x for x in range(dsn_limits.W) for y in range(5) if px[x, y] != (0, 0, 0)]
        measured = dsn_render_text.text_width(text)
        assert (max(lit) + 1 if lit else 0) == measured, \
            f"{text!r}: drew {max(lit) + 1 if lit else 0}px, measured {measured}px"


# --- the data we were throwing away ---------------------------------------

RICH_FEED = b"""<dsn>
 <station name="cdscc" friendlyName="Canberra" timeUTC="1786046106000" timeZoneOffset="36000000.0"/>
 <dish name="DSS43" azimuthAngle="8" elevationAngle="32" isMSPA="true" isArray="true" isDDOR="true">
  <upSignal active="true" signalType="data" dataRate="0" frequency="0" band="S" power="18.0" spacecraft="WIND" spacecraftID="-8"/>
  <downSignal active="true" signalType="data" dataRate="73510" frequency="0" band="S" power="-120.0" spacecraft="WIND" spacecraftID="-8"/>
  <downSignal active="true" signalType="data" dataRate="6400" frequency="0" band="S" power="-140.0" spacecraft="WIND" spacecraftID="-8"/>
  <target name="WIND" id="8" uplegRange="1400000" downlegRange="1400000" rtlt="-1"/>
 </dish>
</dsn>"""


def test_every_simultaneous_receive_record_is_preserved_without_a_false_total():
    """Receiver records may be independent links or redundant processing."""
    link = dsn_source.parse_feed(RICH_FEED)[0]
    assert link.streams == 2
    assert link.down_bps is None
    assert [stream.bps for stream in link.down_streams] == [6400.0, 73510.0]


def test_negative_received_power_survives_parsing():
    """Received power is negative by nature. Routed through _f() -- which
    treats every negative as the feed's no-data sentinel -- the best number
    in the whole feed would silently become zero. Same trap as timezones."""
    link = dsn_source.parse_feed(RICH_FEED)[0]
    assert link.down_dbm == -120.0, "should keep the STRONGEST of the streams"
    assert link.up_kw == 18.0


def test_the_rare_flags_are_read():
    link = dsn_source.parse_feed(RICH_FEED)[0]
    assert (link.mspa, link.arrayed, link.ddor) == (True, True, True)
    text = dsn_audio_words.spoken(link, {}, {})
    assert "more than one dish" in text.lower()
    assert "several spacecraft" in text.lower()
    assert "quasar" in text.lower()
    plain = dsn_source.parse_feed(FEED)[0]
    assert not any((plain.mspa, plain.arrayed, plain.ddor))


def test_power_is_said_in_units_a_person_can_feel():
    assert dsn_audio_words.power_words(-140.0) == "10 attowatts"      # 1e-17 W, Juno
    assert dsn_audio_words.power_words(-130.0) == "100 attowatts"
    assert dsn_audio_words.power_words(-120.0) == "one femtowatt"     # exactly 1e-15 W
    assert dsn_audio_words.power_words(-110.0) == "10 femtowatts"
    assert dsn_audio_words.power_words(-155.0) == "under one attowatt"   # Voyager
    assert dsn_audio_words.power_words(None) == ""
    assert dsn_audio_words.power_words(0.0) == ""


def test_the_narration_contrasts_what_we_send_with_what_comes_back():
    link = dsn_source.parse_feed(RICH_FEED)[0]
    text = dsn_audio_words.spoken(link, {}, {})
    assert "attowatt" in text or "femtowatt" in text
    assert "18 kilowatts" in text
    assert "active receive signal records" in text
    assert "receiver redundancy" in text
    assert "separate streams" not in text
    for ch in set(text.upper()) - {"\n"}:
        assert 32 <= ord(ch) <= 126, f"{ch!r} is not printable ASCII"


def test_units_agree_with_their_numbers():
    """'1 megabits per second' and '0 seconds' both shipped."""
    assert dsn_audio_words.rate_words(1_188_000) == "about 1 megabit per second"
    assert dsn_audio_words.rate_words(2_000_000) == "about 2 megabits per second"
    assert dsn_audio_words.rate_words(1_000) == "about 1 kilobit per second"
    assert dsn_audio_words.light_words(0.43) == "less than a second"   # Chandra
    assert dsn_audio_words.light_words(1.0) == "1 second"


def test_distance_is_not_given_in_light_years():
    """Every craft the DSN talks to is inside the solar system: light-years
    give 0.0022 for Voyager 2 and 0.0000000 for Chandra, so every one would
    read 'zero point zero zero zero'."""
    voyager = dsn_audio_words.distance_words(2.1e10)
    assert "21 billion kilometres away" in voyager
    assert "140 times the Earth's distance from the Sun" in voyager
    assert "light year" not in voyager
    assert dsn_audio_words.distance_words(9.41e8).startswith("941 million kilometres away")
    assert dsn_audio_words.distance_words(111_000) == "111 thousand kilometres away"
    assert dsn_audio_words.distance_words(None) == "" and dsn_audio_words.distance_words(0) == ""
    # the AU comparison is pointless below about one, and is left off
    assert "times the Earth" not in dsn_audio_words.distance_words(1.5e6)


def test_the_light_year_is_used_as_a_comparison_not_a_unit():
    """Saying a light year is 10,000x further than Juno is true and limp.
    Said about the most distant thing we have ever built, it is the point."""
    assert dsn_audio_words.lightyear_words(2.1e10).startswith("A single light year is 451")
    assert "nearest star" in dsn_audio_words.lightyear_words(2.1e10)
    assert dsn_audio_words.lightyear_words(9.41e8) == "", "Juno is not deep space"
    assert dsn_audio_words.lightyear_words(None) == ""


def test_the_distance_sentence_reads_as_english():
    """'941 million kilometres, 6 times the Earth's distance from the Sun
    away' stranded the preposition."""
    link = _voice_link(range_km=2.1e10)
    text = dsn_audio_words.spoken(link, {"vgr2": "Voyager 2"}, {})
    assert "Sun away" not in text
    assert "It is 21 billion kilometres away, 140 times" in text


def test_the_mission_blurbs_cover_what_the_network_actually_tracks():
    """Perseverance turned up live with no description. The blurb table is
    hand-written, so it drifts behind the fleet unless something checks."""
    must_have = {
        "m20", "msl", "wind", "vgr1", "vgr2", "jno", "mro", "m01o", "mvn",
        "lro", "soho", "ace", "psyc", "nhpc", "lucy", "eurc", "spp", "jwst",
        "chdr", "dsco", "tess", "orx", "tgo", "mex", "emm", "sta", "bepi",
    }
    missing = must_have - set(dsn_missions.MISSIONS)
    assert not missing, f"no mission blurb for {sorted(missing)}"


def test_blurbs_are_speakable_and_read_as_sentences():
    for code, blurb in dsn_missions.MISSIONS.items():
        assert blurb == blurb.strip(), f"{code}: stray whitespace"
        assert not blurb.endswith("."), f"{code}: spoken() adds the full stop"
        assert blurb[0].isupper(), f"{code}: should start a sentence"
        for ch in blurb:
            assert 32 <= ord(ch) <= 126, f"{code}: {ch!r} is not ASCII"


def test_an_unknown_craft_still_narrates():
    """A code with no blurb must not produce a gap or a crash — the network
    picks up new missions constantly."""
    link = _voice_link(craft="ZZZZ")
    text = dsn_audio_words.spoken(link, {"zzzz": "Some New Probe"}, {})
    assert "Some New Probe" in text
    assert text.count("  ") == 0, "a missing blurb left a double space"


def _globe_pixels(frame):
    """The disc's COLOURS. Which pixels are lit barely changes as it turns —
    the disc is a fixed circle — so a lit/unlit comparison sees nothing."""
    px = frame.load()
    return tuple(px[x, y]
                 for x in range(dsn_render_palette.GLOBE_CX - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 1)
                 for y in range(dsn_render_palette.GLOBE_CY - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CY + dsn_render_palette.GLOBE_R + 1))


def _night_fraction(frame):
    """How much of the disc is on the dark side. Night is dimmed to 28%, so
    dark pixels are a good proxy for where the terminator sits."""
    px = frame.load()
    disc = [(x, y)
            for x in range(dsn_render_palette.GLOBE_CX - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 1)
            for y in range(dsn_render_palette.GLOBE_CY - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CY + dsn_render_palette.GLOBE_R + 1)
            if px[x, y] != (0, 0, 0)]
    dark = [p for p in disc if sum(px[p]) < 220]
    return len(dark) / max(1, len(disc))


def test_the_globe_turns_in_both_modes():
    """A locked scene follows a message that moves one pixel every 27 minutes.
    Without the planet turning there is nothing alive on the strip at all."""
    link = _voice_link(range_km=2.1e10)
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    sites = {"Canberra": 149.0}
    for label, kwargs in (("browsing", {}),
                          ("locked", {"realtime_since": when.timestamp()})):
        frames, _, _ = dsn_render_distance.render_frames(
            link, when, {}, site_lons=sites, **kwargs)
        assert len({_globe_pixels(f) for f in frames}) > 20, \
            f"the globe barely turns while {label}"


def test_the_shadow_turns_WITH_the_planet():
    """Night belongs to the geography, not to the screen.

    Pinning the terminator to screen space was wrong twice over: the shadow
    sat still while the planet moved under it, and at the hours when the
    centred complex was in full day or full night the disc showed no
    terminator at all for the entire loop. Measured then: 0% lit on every
    frame at 08h UTC, 100% on every frame at 20h.
    """
    link = _voice_link(complex_name="Goldstone", range_km=8.2e8)
    sites = {"Goldstone": -116.9}
    for hour in (2, 8, 14, 20):
        when = datetime(2026, 8, 6, hour, tzinfo=timezone.utc)
        frames, _, _ = dsn_render_distance.render_frames(link, when, {}, site_lons=sites)
        lit = [_lit_fraction(f) for f in frames]
        assert max(lit) - min(lit) > 0.5, (
            f"{hour:02d}h: the shadow did not move with the globe "
            f"({min(lit):.2f}..{max(lit):.2f})")
        assert min(lit) < 0.2 and max(lit) > 0.8, (
            f"{hour:02d}h: never shows a full night or a full day side")


def _lit_fraction(frame):
    px = frame.load()
    disc = [(x, y)
            for x in range(dsn_render_palette.GLOBE_CX - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 1)
            for y in range(dsn_render_palette.GLOBE_CY - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CY + dsn_render_palette.GLOBE_R + 1)
            if px[x, y] != (0, 0, 0)]
    return sum(1 for p in disc if sum(px[p]) >= 220) / len(disc)


def test_the_browsing_spin_does_not_depend_on_the_spacecraft():
    """The original bug: the globe was tied to a loop whose length was the
    crossing time, so Earth span faster whenever a nearer craft came up."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    near = _voice_link(craft="LRO", range_km=3.8e5)
    far = _voice_link(craft="VGR2", range_km=2.1e10)

    def spin_of(link):
        frames, _, _ = dsn_render_distance.render_frames(link, when, {})
        px0, px1 = frames[0].load(), frames[10].load()
        # stop one column short of the limb: the arrival flare deliberately
        # lands on GLOBE_CX + GLOBE_R, and WHEN it lands depends on the
        # crossing time, which is exactly the craft-dependence being excluded
        return sum(1 for x in range(dsn_render_palette.GLOBE_CX - dsn_render_palette.GLOBE_R,
                                    dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R)
                   for y in range(dsn_render_palette.GLOBE_CY - dsn_render_palette.GLOBE_R,
                                  dsn_render_palette.GLOBE_CY + dsn_render_palette.GLOBE_R + 1)
                   if px0[x, y] != px1[x, y])

    assert spin_of(near) == spin_of(far), "Earth's spin follows the spacecraft"


def test_the_globe_is_centred_on_the_complex_that_is_listening():
    """So the lit or dark face is the real day or night AT THAT ANTENNA.

    The camera used to drift: the view centre advanced 15 deg/hr while the
    subsolar point retreated 15 deg/hr, so they lapped each other and the disc
    went fully lit to fully dark and back TWICE a day. No vantage point does
    that. Measured before the fix: 0% lit at 00:00 and 12:00 UTC, 100% at
    06:00 and 18:00.
    """
    sites = {"Goldstone": -116.9, "Canberra": 149.0}

    def lit_fraction(complex_name, hour):
        link = _voice_link(complex_name=complex_name, range_km=2.1e10)
        when = datetime(2026, 8, 6, hour, tzinfo=timezone.utc)
        frame = dsn_render_distance.render_frames(link, when, {}, site_lons=sites)[0][0]
        px = frame.load()
        disc = [(x, y)
                for x in range(dsn_render_palette.GLOBE_CX - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 1)
                for y in range(dsn_render_palette.GLOBE_CY - dsn_render_palette.GLOBE_R, dsn_render_palette.GLOBE_CY + dsn_render_palette.GLOBE_R + 1)
                if px[x, y] != (0, 0, 0)]
        return sum(1 for p in disc if sum(px[p]) >= 220) / len(disc)

    # Goldstone is UTC-7: 20:00 UTC is one in the afternoon there.
    assert lit_fraction("Goldstone", 20) > 0.9, "daytime at Goldstone reads as night"
    assert lit_fraction("Goldstone", 8) < 0.1, "1am at Goldstone reads as day"
    # Canberra is UTC+10, so it is the other way round at the same instants.
    assert lit_fraction("Canberra", 2) > 0.9
    assert lit_fraction("Canberra", 14) < 0.1

    # exactly ONE day/night cycle per day, not two
    lit = [lit_fraction("Goldstone", h) for h in range(0, 24, 2)]
    crossings = sum(1 for a, b in zip(lit, lit[1:]) if (a > 0.5) != (b > 0.5))
    assert crossings == 2, f"{crossings} terminator crossings in a day, expected 2"


# --- the top status LED ----------------------------------------------------


def test_the_led_blinks_on_events_not_on_every_redraw():
    """The device API can blink only one colour once per draw. Wired to the
    ordinary refresh it becomes a metronome every REDRAW_S seconds."""
    assert dsn_device_display._scene_payload("x.anim").led_notification_color is None
    assert dsn_device_display._scene_payload("x.anim", dsn_limits.LED_ARRIVAL).led_notification_color \
        == dsn_limits.LED_ARRIVAL
    for colour in (dsn_limits.LED_ARRIVAL, dsn_limits.LED_LOCKED, dsn_limits.LED_RELEASED):
        assert re.fullmatch(r"#[0-9A-Fa-f]{8}", colour), colour


def test_an_arrival_fires_once_and_then_the_watch_is_over():
    """It used to count repeating crossings, because progress wrapped and a
    new message set off each time. The watch ends at the first arrival now."""
    state = dsn_model.State()
    link = _voice_link(range_km=2.1e10)
    light, lock = link.light_s, 1_780_000_000.0
    state.realtime_since, state.focus = lock, link.key

    assert dsn_device_scene_policy.arrival_due(state, link, lock) is False
    assert dsn_device_scene_policy.arrival_due(state, link, lock + light * 0.5) is False
    assert dsn_device_scene_policy.arrival_due(state, link, lock + light + 1) is True
    assert dsn_device_scene_policy.arrival_due(state, link, lock + light * 2) is False


def test_browsing_never_reports_an_arrival():
    state = dsn_model.State()
    link = _voice_link(range_km=2.1e10)
    assert state.realtime_since is None
    for t in (0.0, 1e9, 2e9):
        assert dsn_device_scene_policy.arrival_due(state, link, t) is False
    assert state.rt_generation is None


def test_a_blink_is_consumed_rather_than_repeated():
    """Left set, every subsequent draw would blink again."""
    state = dsn_model.State()
    state.led_blink = dsn_limits.LED_LOCKED
    led, state.led_blink = state.led_blink, None
    assert led == dsn_limits.LED_LOCKED
    assert state.led_blink is None


# --- spacecraft silhouettes ------------------------------------------------


def test_no_sprite_is_a_solid_blob():
    """The same rule the font learned: on a panel whose LEDs are spaced nearly
    their own width apart, the densest shape is the one that loses its
    outline. The first set drew a filled dish at 27 of 35 and a filled shield
    at 29, and both read as coloured blocks."""
    cells = dsn_render_craft.CRAFT_W * dsn_render_craft.CRAFT_H
    for name, sprite in dsn_render_craft.SPRITES.items():
        assert len(sprite) == dsn_render_craft.CRAFT_H, f"{name}: sprites are 6 tall"
        assert {len(r) for r in sprite} == {dsn_render_craft.CRAFT_W}, f"{name}: ragged"
        ink = sum(1 for row in sprite for ch in row if ch != ".")
        assert ink / cells < 0.55, f"{name} is {ink}/{cells} — reads as a blob"
        # cubesat is deliberately the sparsest thing in the fleet: being tiny
        # beside everything else IS its likeness
        floor = 6 if name == "cubesat" else 14
        assert ink >= floor, f"{name} is only {ink}/{cells} — too sparse"
        for ch in {c for row in sprite for c in row} - {"."}:
            assert ch in dsn_render_craft.INK, f"{name}: no colour for {ch!r}"


def test_the_sprite_box_stays_inside_the_panel():
    """It sits hard against the right edge, so a wider sprite silently loses
    its tail rather than erroring."""
    assert dsn_render_craft.CRAFT_X + dsn_render_craft.CRAFT_W == dsn_limits.W
    assert dsn_render_craft.CRAFT_Y + dsn_render_craft.CRAFT_H == dsn_limits.H - 5   # clear of the bottom label
    assert dsn_render_craft.CRAFT_Y == 5                          # clear of the top label


def test_the_silhouettes_are_actually_different_from_each_other():
    """Shipping shapes nobody can tell apart is worse than one honest generic.
    Craft that genuinely look alike share a sprite on purpose."""
    shapes = list(dsn_render_craft.SPRITES.items())
    for i, (a, sa) in enumerate(shapes):
        for b, sb in shapes[i + 1:]:
            diff = sum(1 for ra, rb in zip(sa, sb)
                       for ca, cb in zip(ra, rb) if ca != cb)
            assert diff >= 10, f"{a} and {b} differ by only {diff} of 66 cells"


def test_craft_map_to_the_shape_they_actually_are():
    """The point of the set is likeness, so the mapping is the deliverable."""
    assert dsn_render_craft.craft_sprite("VGR2") == dsn_render_craft.SPRITES["voyager"]
    assert dsn_render_craft.craft_sprite("VGR1") == dsn_render_craft.craft_sprite("VGR2")  # identical
    assert dsn_render_craft.craft_sprite("JNO") == dsn_render_craft.SPRITES["juno"]
    assert dsn_render_craft.craft_sprite("LUCY") == dsn_render_craft.SPRITES["lucy"]
    assert dsn_render_craft.craft_sprite("SPP") == dsn_render_craft.SPRITES["parker"]
    assert dsn_render_craft.craft_sprite("JWST") == dsn_render_craft.SPRITES["jwst"]
    assert dsn_render_craft.craft_sprite("MRO") == dsn_render_craft.SPRITES["marsorbiter"]
    # the two on a surface are not orbiters and must not look like one
    assert dsn_render_craft.craft_sprite("M20") == dsn_render_craft.SPRITES["rover"]
    assert dsn_render_craft.craft_sprite("MSL") == dsn_render_craft.SPRITES["rover"]
    assert dsn_render_craft.craft_sprite("MER1") == dsn_render_craft.SPRITES["solarrover"]
    assert dsn_render_craft.craft_sprite("M20") != dsn_render_craft.craft_sprite("MRO")
    # an Artemis upper stage is not a spacecraft and should not pretend to be
    assert dsn_render_craft.craft_sprite("ICPS") == dsn_render_craft.SPRITES["stage"]
    # anything unmapped still draws a plausible satellite
    assert dsn_render_craft.craft_sprite("ZZZZ") == dsn_render_craft.SPRITES[dsn_render_craft.DEFAULT_SHAPE]
    assert dsn_render_craft.craft_sprite("") == dsn_render_craft.SPRITES[dsn_render_craft.DEFAULT_SHAPE]
    for code in dsn_render_craft.CRAFT_SHAPES:
        assert dsn_render_craft.CRAFT_SHAPES[code] in dsn_render_craft.SPRITES, code


def test_every_sprite_belongs_to_someone():
    """A portrait nobody is mapped to is a portrait of nothing."""
    used = set(dsn_render_craft.CRAFT_SHAPES.values()) | {dsn_render_craft.DEFAULT_SHAPE}
    assert set(dsn_render_craft.SPRITES) == used, f"orphans: {set(dsn_render_craft.SPRITES) - used}"


def test_the_fleet_is_described_as_well_as_drawn():
    """A craft with a portrait and no description is half a scene. MISSIONS
    is the source of truth for who exists; the mapping must not drift off it
    with typos nobody notices."""
    unknown = sorted(c for c in dsn_render_craft.CRAFT_SHAPES if c not in dsn_missions.MISSIONS)
    assert not unknown, f"drawn but never described: {unknown}"


def test_the_sprite_reaches_the_panel():
    """Wired into render_frames, not just defined."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    juno = dsn_render_distance.render_frames(_voice_link(craft="JNO", range_km=8.2e8), when, {})[0][0]
    vgr = dsn_render_distance.render_frames(_voice_link(craft="VGR2", range_km=2.1e10), when, {})[0][0]

    def craft_area(frame):
        px = frame.load()
        return tuple(px[x, y] for x in range(62, dsn_limits.W)
                     for y in range(dsn_render_palette.TRACK_Y - 2, dsn_render_palette.TRACK_Y + 3))

    assert craft_area(juno) != craft_area(vgr), "both craft drew the same shape"


def _first_moves(frames, row, colour):
    """Where the pulse sits in the first two frames that HAVE one — a near
    craft's chain leaves its row empty for most of the loop."""
    seen = [min(xs) for xs in
            ([x for x in range(dsn_limits.TRACK0, dsn_limits.TRACK1 + 1)
              if f.load()[x, row] == colour] for f in frames) if xs]
    assert len(seen) >= 2, "no pulse anywhere in the loop"
    return seen[1] - seen[0]


def test_both_halves_of_the_conversation_are_drawn():
    """A pass is bidirectional -- Perseverance had an active uplink AND
    downlink on DSS-34 while this was written. The renderer used to draw only
    one direction, so half of it was simply invisible."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    two_way = _voice_link(down_bps=1.5e6, up_active=True, range_km=2.95e8)
    frames, _, _ = dsn_render_distance.render_frames(two_way, when, {})

    def row_has(colour, row):
        return any(f.load()[x, row] == colour
                   for f in frames for x in range(dsn_limits.TRACK0, dsn_limits.TRACK1 + 1))

    assert row_has(dsn_render_palette.UPLINK, dsn_render_palette.UP_Y), "Earth's half is missing"
    assert row_has(dsn_render_palette.PULSE, dsn_render_timing.DOWN_Y), "the spacecraft's half is missing"
    assert dsn_render_palette.UP_Y != dsn_render_timing.DOWN_Y, "the two directions must not share a row"
    # and each must travel the right way
    assert _first_moves(frames, dsn_render_palette.UP_Y, dsn_render_palette.UPLINK) > 0, "uplink must go out"
    assert _first_moves(frames, dsn_render_timing.DOWN_Y, dsn_render_palette.PULSE) < 0, "downlink comes home"


def test_the_uplink_outweighs_what_comes_back():
    """We answer at tens of KILOWATTS; what returns is attowatts. That ratio
    is about 10^18 and the contrast is the whole story, so the uplink is
    drawn heavy and bright and the downlink thin."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    link = _voice_link(down_bps=160.0, up_active=True, range_km=2.1e10)
    frames, _, _ = dsn_render_distance.render_frames(link, when, {})

    def widest(row, colour):
        best = 0
        for f in frames:
            px, run = f.load(), 0
            for x in range(dsn_limits.TRACK0, dsn_limits.TRACK1 + 1):
                run = run + 1 if px[x, row] == colour else 0
                best = max(best, run)
        return best

    assert widest(dsn_render_palette.UP_Y, dsn_render_palette.UPLINK) > widest(dsn_render_timing.DOWN_Y, dsn_render_palette.PULSE)
    assert sum(dsn_render_palette.UPLINK) > sum(dsn_render_palette.PULSE) * 0.8, "the uplink must read brighter"
    # both stay above the panel's gamma floor against their own tether
    for ink, tether in ((dsn_render_palette.UPLINK, dsn_render_palette.UP_TETHER), (dsn_render_palette.PULSE, dsn_render_palette.TETHER)):
        assert sum(ink) > sum(tether) * 3, "pulse and tether will look alike"


def test_a_one_way_pass_still_draws_its_single_direction():
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    up_only = _voice_link(down_bps=0.0, up_active=True, range_km=2.95e8)
    frames, _, _ = dsn_render_distance.render_frames(up_only, when, {})
    assert _first_moves(frames, dsn_render_palette.UP_Y, dsn_render_palette.UPLINK) > 0
    # the quiet direction still shows a link, it just has nothing on it
    assert all(f.load()[dsn_limits.TRACK0, dsn_render_timing.DOWN_Y] != (0, 0, 0) for f in frames)


def test_the_tether_joins_earth_to_the_craft_at_all_times():
    """Discrete marks alone read as one shooting the other. A dim line that
    is always there makes them read as linked, with the signal running along
    it."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    frames, _, _ = dsn_render_distance.render_frames(_voice_link(range_km=2.95e8), when, {})
    for f in frames:
        px = f.load()
        unlit = [x for x in range(dsn_limits.TRACK0, dsn_limits.TRACK1 + 1)
                 if px[x, dsn_render_palette.TRACK_Y] == (0, 0, 0)]
        assert not unlit, f"the link had a gap in it at {unlit[:5]}"
    # and the tether must sit far under the pulse, or it becomes the signal
    assert sum(dsn_render_palette.TETHER) * 3 < sum(dsn_render_palette.PULSE)


def test_the_pulse_length_carries_the_data_rate():
    """Spacing already carries distance. This puts the other half of the link
    on screen: a 2 Mbit downlink is a fat dash, Voyager's 160 bps a flicker."""
    assert dsn_render_carriers.pulse_span(2e6) == 3
    assert dsn_render_carriers.pulse_span(18090) == 2
    assert dsn_render_carriers.pulse_span(160) == 1
    assert dsn_render_carriers.pulse_span(0) == 1

    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)

    def widest(bps):
        frames, _, _ = dsn_render_distance.render_frames(
            _voice_link(down_bps=bps, range_km=2.95e8), when, {})
        runs = []
        for f in frames:
            px, run = f.load(), 0
            for x in range(dsn_limits.TRACK0, dsn_limits.TRACK1 + 1):
                run = run + 1 if px[x, dsn_render_palette.TRACK_Y] == dsn_render_palette.PULSE else 0
                runs.append(run)
        return max(runs)

    assert widest(2e6) > widest(160), "a fat downlink should draw a fat pulse"


def test_the_countdown_says_how_long_until_it_lands():
    """It replaced a clock showing when the signal DEPARTED, which answered a
    question nobody asked. This one needs no explaining."""
    light, lock = 71400.0, 1_780_000_000.0          # Voyager, 19h48m
    assert dsn_render_timing.countdown_label(light, lock, lock) == "19:50"
    assert dsn_render_timing.countdown_label(light, lock, lock + 3600) == "18:50"
    assert dsn_render_timing.countdown_label(960.0, lock, lock) == "16:00"        # Mars
    assert dsn_render_timing.countdown_label(960.0, lock, lock + 900) == "1:00"
    assert dsn_render_timing.countdown_label(960.0, lock, lock + 960) == "0:00"
    assert dsn_render_timing.countdown_label(None, lock, lock) == "?"
    for label in (dsn_render_timing.countdown_label(light, lock, lock),
                  dsn_render_timing.countdown_label(960.0, lock, lock + 930)):
        assert len(label) <= 5, label
        for ch in label:
            assert ch in dsn_render_text.FONT, f"{ch!r} would be silently skipped"


def test_the_countdown_really_does_finish():
    """19h48m is not 'never' -- it is tomorrow morning, on a device that runs
    all day. The arrival is a real event, not a compressed imitation."""
    light, lock = 71400.0, 1_780_000_000.0
    assert not dsn_render_timing.arrived(light, lock, lock)
    assert not dsn_render_timing.arrived(light, lock, lock + light - 1)
    assert dsn_render_timing.arrived(light, lock, lock + light)
    assert not dsn_render_timing.arrived(None, lock, lock + 1e9)


def test_progress_stops_at_arrival_instead_of_wrapping():
    """The watch ends when the signal lands; it must not silently restart."""
    light, lock = 1000.0, 1_780_000_000.0
    assert dsn_render_timing.realtime_progress(light, lock + 500, lock) == pytest.approx(0.5)
    assert dsn_render_timing.realtime_progress(light, lock + 1500, lock) == 1.0


def test_the_arrival_ends_the_watch_and_hands_back_the_rotation():
    """The countdown running out is the event: signal lands, LED blinks, and
    the strip returns to cycling the live feed."""
    state = dsn_model.State()
    link = _voice_link(range_km=2.95e8)              # Mars, ~16 min
    lock = 1_780_000_000.0
    state.realtime_since, state.focus = lock, link.key

    assert dsn_device_scene_policy.arrival_due(state, link, lock + 60) is False
    assert state.focus == link.key, "released before it landed"

    assert dsn_device_scene_policy.arrival_due(state, link, lock + link.light_s + 1) is True
    assert state.realtime_since is None, "still locked after arrival"
    assert state.focus is None, "did not hand back to the rotation"
    assert dsn_device_scene_policy.arrival_due(state, link, lock + 1e6) is False   # only once


def test_every_spacecraft_the_network_publishes_has_a_description():
    """SWFO and Perseverance both turned up live with nothing to say about
    them, because the table only covered craft that had already appeared.
    This pins the whole published list, so the next new mission fails here
    rather than on the panel."""
    import xml.etree.ElementTree as ET
    fixture = Path(__file__).parent / "fixtures" / "dsn_config.xml"
    if not fixture.exists():                    # no network in CI: skip
        pytest.skip("no cached config.xml fixture")
    root = ET.fromstring(fixture.read_bytes())
    published = {el.get("name", "").lower()
                 for el in root.iter("spacecraft") if el.get("name")}
    real = {c for c in published if c.upper() not in dsn_source.NOT_SPACECRAFT}
    missing = sorted(real - set(dsn_missions.MISSIONS))
    assert not missing, f"no description for {missing}"


def test_a_line_is_not_baked_before_the_data_it_describes_arrives():
    """spoken() drops whole sentences when its inputs are missing, and the
    content hash then caches the stub forever. Measured on device: complete
    narrations run 15-25s, the stubs baked this way ran 1.4 to 4.6s."""
    state = dsn_model.State()
    link = _voice_link(range_km=None, naif=-32)

    assert not dsn_audio_policy.narration_ready(state, link), "baked before the name table"
    state.names = {"vgr2": "Voyager 2"}
    assert not dsn_audio_policy.narration_ready(state, link), "baked before Horizons answered"

    link.range_km = 2.1e10
    assert dsn_audio_policy.narration_ready(state, link)

    # a craft whose id never resolves must not be blocked forever
    assert dsn_audio_policy.narration_ready(state, _voice_link(range_km=None, naif=None))

    # and the gated line really is the fuller one
    thin = dsn_audio_words.spoken(_voice_link(range_km=None), {}, {})
    full = dsn_audio_words.spoken(_voice_link(range_km=2.1e10), {"vgr2": "Voyager 2"}, {})
    assert len(full.split()) > len(thin.split()) + 15


def test_bumping_the_cache_generation_retires_the_old_one():
    """Every line baked by the ungated path has to go, or it is served
    forever. The generation prefix makes the existing sweep do it."""
    old = "voice_afnova_0f76f95857.snd"          # previous generation
    assert not dsn_audio_assets.VOICE_FILES.match(old), "old generation still looks current"
    assert dsn_audio_assets.VOICE_FILES.match(dsn_audio_assets.speech_name("x", "af_nova"))


def test_distances_survive_a_restart(tmp_path, monkeypatch):
    """Nothing was persisted, so every deploy re-fetched every distance from
    a public API -- and once baking was gated on a resolved range, a restart
    meant no narration until JPL answered."""
    monkeypatch.setattr(dsn_settings, "RANGE_CACHE", tmp_path / "r.json")
    state = dsn_model.State()
    state.ranges = {-32: (2.1e10, time.time())}
    dsn_ranges.save_ranges(state)

    fresh = dsn_model.State()
    dsn_ranges.load_ranges(fresh)
    assert fresh.ranges[-32][0] == 2.1e10

    # anything past the TTL is dropped rather than served stale
    stale = dsn_model.State()
    stale.ranges = {-61: (8.2e8, time.time() - dsn_limits.RANGE_TTL_S - 1)}
    dsn_ranges.save_ranges(stale)
    reloaded = dsn_model.State()
    dsn_ranges.load_ranges(reloaded)
    assert -61 not in reloaded.ranges

    # a missing or corrupt cache is a cold start, never a crash
    (tmp_path / "r.json").write_text("{not json")
    empty = dsn_model.State()
    dsn_ranges.load_ranges(empty)
    assert empty.ranges == {}


def test_the_name_map_keeps_trying(monkeypatch):
    """It ran once. A transient failure left every craft showing its bare feed
    code, every dish claiming 34 metres, and the globe centred on longitude
    zero -- and it looked almost normal."""
    calls = []

    async def flaky(state):
        calls.append(1)
        if len(calls) < 3:
            return False
        state.names = {"vgr2": "Voyager 2"}
        return True

    monkeypatch.setattr(dsn_feed, "fetch_names", flaky)
    sleeps = []

    async def no_wait(secs):
        sleeps.append(secs)
        if len(sleeps) > 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    state = dsn_model.State()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn_feed.poll_names(state))

    assert len(calls) >= 3, "gave up after the first failure"
    assert state.names, "never recovered the name map"
    assert sleeps[0] < sleeps[1], "did not back off between retries"
    assert max(sleeps) >= 3600, "did not settle into a periodic refresh"


def test_the_antenna_leans_the_way_the_real_dish_leans():
    """Elevation was parsed for every link and spent only on a log line. The
    icon is now the place it shows: near the horizon at the start and end of
    a pass, straight up in the middle of one."""
    low = dsn_render_dish.dish_tilt(12.0)
    mid = dsn_render_dish.dish_tilt(41.0)
    high = dsn_render_dish.dish_tilt(85.0)
    assert low != mid != high and low != high, "three elevations, one picture"
    # a parked dish reports 90 with no target, but NOT_SPACECRAFT drops it
    # long before a Link exists, so 90 here means genuinely overhead
    assert dsn_render_dish.dish_tilt(90.0) == high
    assert dsn_render_dish.dish_tilt(0.0) == low                # and a missing angle
    assert dsn_render_dish.dish_tilt(120.0) == high             # never falls off the end


def test_the_antenna_icon_cannot_silently_steal_the_name_box():
    """Every column of icon costs a column of the spacecraft name's scroll
    window. Widening it is allowed; widening it by accident is not."""
    assert dsn_render_dish.DISH_ICON_W == 6
    for ceiling, rows in dsn_render_dish.DISH_TILTS:
        assert all(len(r) == dsn_render_dish.DISH_ICON_W for r in rows), ceiling
        # six rows fits y0..y5; y6 is the uplink tether's row
        assert len(rows) <= 6, ceiling


def test_the_antenna_is_not_drawn_in_the_digits_colour():
    """In DISH_NO the glyph reads as another character in '43'. The hue IS
    the fix — a shape change alone did not survive the panel."""
    assert dsn_render_dish.ANTENNA != dsn_render_palette.DISH_NO
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    frames, _, _ = dsn_render_distance.render_frames(_voice_link(elevation=12.0), when, {})
    px = frames[0].load()
    tx = dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 3
    drawn = {px[tx + dx, dy]
             for dy in range(6) for dx in range(dsn_render_dish.DISH_ICON_W)}
    assert dsn_render_dish.ANTENNA in drawn, "the antenna icon never reached the panel"


def test_the_antenna_icon_leaves_the_tether_alone():
    """Six rows is legal only because y5 is empty at this x. A seventh would
    land on the uplink."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    frames, _, _ = dsn_render_distance.render_frames(_voice_link(elevation=85.0), when, {})
    px = frames[0].load()
    tx = dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 3
    for dx in range(dsn_render_dish.DISH_ICON_W):
        assert px[tx + dx, dsn_render_palette.UP_Y] != dsn_render_dish.ANTENNA, "icon spilled onto y6"


def test_a_glint_lands_on_the_craft_and_not_on_empty_space():
    """The highlight is the only motion eleven pixels can carry. Pointed at a
    cell the sprite never lights, it reads as a detached spark instead of sun
    catching an array."""
    for shape, path in dsn_render_craft.CRAFT_GLINT.items():
        sprite = dsn_render_craft.SPRITES[shape]
        for row, col in path:
            assert 0 <= row < dsn_render_craft.CRAFT_H and 0 <= col < dsn_render_craft.CRAFT_W, shape
            assert sprite[row][col] != ".", \
                f"{shape}: glint at ({row},{col}) is off the structure"


def test_only_craft_that_really_spin_are_animated():
    """Inventing motion a craft does not have is the same error as inventing
    a silhouette it does not have. These hold a fixed attitude."""
    for still in ("jwst", "parker", "lucy", "clipper", "rover",
                  "solarrover", "telescope"):
        assert still not in dsn_render_craft.CRAFT_GLINT, f"{still} does not spin"
    assert "juno" in dsn_render_craft.CRAFT_GLINT       # spin-stabilised at 2 rpm
    assert "spinner" in dsn_render_craft.CRAFT_GLINT    # ACE, Wind, IMAP, Ulysses


def test_the_glint_actually_moves_across_the_loop():
    """Baked into forty frames, a highlight that never advances is just a
    brighter pixel."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    frames, _, _ = dsn_render_distance.render_frames(_voice_link(craft="JNO"), when, {})
    spots = set()
    for f in frames:
        px = f.load()
        for y in range(dsn_render_craft.CRAFT_Y, dsn_render_craft.CRAFT_Y + dsn_render_craft.CRAFT_H):
            for x in range(dsn_render_craft.CRAFT_X, dsn_limits.W):
                if px[x, y] == dsn_render_craft.HOT:
                    spots.add((x, y))
    # distinct positions: the path repeats each tip so the sweep runs at
    # Juno's real 2 rpm rather than four times it
    assert len(spots) == len(set(dsn_render_craft.CRAFT_GLINT["juno"])), \
        f"the glint visited {len(spots)} places, not the whole path"


def test_the_sun_is_where_the_sun_actually_is():
    """Declination is not optional: without it the terminator is a great
    circle through both poles all year and the globe sits at a permanent
    equinox — no midnight sun, no polar night."""
    solstice = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)
    lat, lon = dsn_render_globe.subsolar(solstice)
    assert 23.0 < lat < 23.6, f"June solstice declination was {lat}"
    lat, _ = dsn_render_globe.subsolar(datetime(2026, 12, 21, 12, tzinfo=timezone.utc))
    assert -23.6 < lat < -23.0
    for month in (3, 9):
        lat, _ = dsn_render_globe.subsolar(datetime(2026, month, 21, 12, tzinfo=timezone.utc))
        assert abs(lat) < 1.5, f"equinox declination was {lat}"
    # the equation of time: noon UTC is NOT the prime meridian's true noon
    offs = [dsn_render_globe.subsolar(datetime(2026, m, 5, 12, tzinfo=timezone.utc))[1]
            for m in range(1, 13)]
    assert max(offs) - min(offs) > 4.0, "the equation of time is missing"
    assert max(abs(o) for o in offs) < 5.0, "that is too big to be the EoT"


def test_the_arctic_gets_a_midnight_sun_in_july():
    """The whole point of declination. In northern summer the pole never sets;
    in northern winter it never rises."""
    july = datetime(2026, 7, 1, tzinfo=timezone.utc)
    jan = datetime(2027, 1, 1, tzinfo=timezone.utc)
    for hours in range(0, 24, 3):
        j = july.replace(hour=hours)
        lat, lon = dsn_render_globe.subsolar(j)
        # angular distance from the subsolar point to the north pole
        away = 90.0 - lat
        assert away < 80.0, f"north pole in darkness at {j} (sun {away:.0f} off)"
        lat, _ = dsn_render_globe.subsolar(jan.replace(hour=hours))
        assert 90.0 - lat > 95.0, "north pole should be in polar night in January"


def test_the_planet_turns_the_way_the_planet_turns():
    """Earth rotates east, so for an observer fixed in space the longitude at
    the centre of the disc DECREASES. Getting this backwards drifted Greenwich
    the wrong way across the strip, and dragged the terminator with it."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    centres = []
    for step in range(3):
        t = when.replace(minute=step * 20)
        _, lon = dsn_render_globe.subsolar(t)
        centres.append(lon)
    assert centres[0] > centres[1] > centres[2], \
        "the subsolar longitude must fall as the day advances"


def test_the_band_is_finally_said_out_loud():
    """S, X and Ka is the single fact that explains why rates span four orders
    of magnitude, and it was parsed for every link and spent on a log line."""
    assert "Ka band" in dsn_audio_words.band_words("Ka")
    assert "S band" in dsn_audio_words.band_words("S")
    assert "X band" in dsn_audio_words.band_words("X")
    assert dsn_audio_words.band_words("") == ""
    text = dsn_audio_words.spoken(_voice_link(band="Ka"), {"vgr2": "Voyager 2"})
    assert "Ka band" in text


def test_the_band_colours_the_downlink():
    """Ka is why Mars sends pictures and Voyager sends numbers. All three stay
    warm so none can be mistaken for the cold blue uplink."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    seen = {}
    for band in ("S", "X", "Ka"):
        frames, _, _ = dsn_render_distance.render_frames(_voice_link(band=band), when, {})
        px = frames[0].load()
        lit = {px[x, dsn_render_timing.DOWN_Y] for x in range(dsn_limits.TRACK0, dsn_limits.TRACK1 + 1)}
        seen[band] = lit
    assert seen["S"] != seen["X"] != seen["Ka"], "the bands look identical"
    for band, colour in dsn_render_palette.BAND_PULSE.items():
        r, g, b = colour
        assert r > b, f"{band} is not warm — it will read as an uplink"


def test_a_seventy_metre_dish_looks_like_one():
    """DSS-14, 43 and 63 have four times the collecting area of a 34 m, and
    Voyager can essentially only be heard by those three."""
    small = dsn_render_dish.dish_tilt(41.0, big=False)
    big = dsn_render_dish.dish_tilt(41.0, big=True)
    assert small != big
    assert sum(r.count("1") for r in big) > sum(r.count("1") for r in small)
    # the footprint must not change, or the name's scroll window moves
    for rows in (small, big):
        assert len(rows) == 6 and {len(r) for r in rows} == {dsn_render_dish.DISH_ICON_W}


def test_arraying_and_mspa_are_drawn_not_just_narrated():
    """A four-dish array used to look exactly like a single dish."""
    when = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    plain, _, _ = dsn_render_distance.render_frames(_voice_link(), when, {})
    for flag, colour in (("arrayed", dsn_render_dish.ANTENNA), ("mspa", dsn_render_palette.NAME),
                         ("ddor", dsn_render_dish.DDOR_MARK)):
        marked, _, _ = dsn_render_distance.render_frames(_voice_link(**{flag: True}), when, {})
        col = {marked[0].load()[dsn_render_dish.MODE_X, y] for y in range(5)}
        assert colour in col, f"{flag} left no mark"
        assert col != {plain[0].load()[dsn_render_dish.MODE_X, y] for y in range(5)}


def test_round_trip_time_is_the_number_an_operator_lives_by():
    """Round-trip is light-time context, not a promised response deadline."""
    text = dsn_audio_words.spoken(_voice_link(range_km=2.1e10), {"vgr2": "Voyager 2"})
    assert "light-time alone for an immediate round trip" in text
    assert "38 hours and 55 minutes" in text, text   # 19h27m, doubled
    assert "would not be answered" not in text
    # ...and not for something a light-second away, where it is noise
    assert "immediate round trip" not in dsn_audio_words.spoken(
        _voice_link(range_km=3.8e5), {})


def test_every_band_the_feed_publishes_is_handled():
    """Built from an assumed vocabulary rather than the feed's actual one, the
    first version handled Ka and silently dropped K — the fastest band on the
    network, drawn in default amber and narrated in silence.

    These four are what NASA's live feed emits."""
    for band in ("S", "X", "K", "Ka"):
        assert dsn_render_palette.BAND_PULSE.get(dsn_source.band_key(band)), f"{band} has no colour"
        assert dsn_audio_words.band_words(band), f"{band} is narrated in silence"
    # and the shapes the string arrives in
    assert dsn_source.band_key(" ka ") == dsn_source.band_key("Ka") == dsn_source.band_key("KA")
    assert dsn_source.band_key("Ka-band") == "KA"
    assert dsn_source.band_key("") == ""
    assert dsn_audio_words.band_words("") == ""          # unknown stays quiet, not wrong


def test_the_high_bands_are_related_but_physically_distinguishable():
    """K and Ka are neighbours, but the live instrument names them separately.
    The panel needs a full measured contrast step or that distinction is fake."""
    k, ka = dsn_render_palette.BAND_PULSE["K"], dsn_render_palette.BAND_PULSE["KA"]
    assert k != ka
    assert max(abs(a - b) for a, b in zip(k, ka)) >= 77
    assert k[0] > k[2] and ka[0] > ka[2]
    warm = dsn_render_palette.BAND_PULSE["KA"]
    assert warm[0] > warm[2], "still warm — must not read as the blue uplink"


# --- link history ----------------------------------------------------------


def _snap(*pairs, **flags):
    """A snapshot of links, as (dish, craft) pairs."""
    return [dsn_source.Link(complex_name="Canberra", dish=d, craft=c, elevation=40.0,
                     band="X", down_bps=1000.0, up_active=True,
                     range_km=1e8, **flags)
            for d, c in pairs]


def test_history_records_transitions_not_polls():
    """A poll every 30 seconds would be 2,880 identical lines a day. What is
    worth remembering is the change."""
    before = _snap(("DSS43", "VGR2"), ("DSS14", "JNO"))
    after = _snap(("DSS43", "VGR2"), ("DSS26", "EURC"))
    events = dsn_history.link_events(before, after, 1000.0)
    kinds = {(e["event"], e["craft"]) for e in events}
    assert ("appear", "EURC") in kinds
    assert ("vanish", "JNO") in kinds
    # VGR2 was there before and is there now: not an event
    assert not any(e["craft"] == "VGR2" for e in events)
    # a steady network produces nothing at all
    assert dsn_history.link_events(after, after, 1000.0) == []


def test_history_notices_a_pass_changing_character():
    """An array forming or a DDOR fix starting mid-pass is the interesting
    moment, and it is invisible if only appear/vanish are logged."""
    before = _snap(("DSS43", "VGR2"))
    after = _snap(("DSS43", "VGR2"), arrayed=True, ddor=True)
    events = dsn_history.link_events(before, after, 1000.0)
    assert len(events) == 1
    assert events[0]["event"] == "flags"
    assert events[0]["flags"] == ["arrayed", "ddor"]


def test_history_survives_a_restart(tmp_path, monkeypatch):
    """The point of the file. Without it the app forgets every pass the moment
    the process dies, which on a Pi is every deploy."""
    monkeypatch.setattr(dsn_settings, "HISTORY_PATH", tmp_path / "h.jsonl")
    dsn_history.append_history(dsn_history.link_events([], _snap(("DSS43", "VGR2")), 100.0))
    dsn_history.append_history(dsn_history.link_events([], _snap(("DSS14", "VGR2")), 200.0))

    state = dsn_model.State()
    dsn_history.load_history(state)
    assert state.seen["vgr2"]["passes"] == 2
    assert state.seen["vgr2"]["first"] == 100.0
    assert state.seen["vgr2"]["last"] == 200.0
    assert "jno" not in state.seen


def test_a_half_written_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    """This file is appended to by a process that can be SIGKILLed mid-write,
    which on a Pi under systemd is a normal Tuesday."""
    path = tmp_path / "h.jsonl"
    monkeypatch.setattr(dsn_settings, "HISTORY_PATH", path)
    path.write_text('{"t": 1, "event": "appear", "craft": "VGR2"}\n'
                    '{"t": 2, "event": "app')          # truncated mid-write
    state = dsn_model.State()
    dsn_history.load_history(state)
    assert state.seen["vgr2"]["passes"] == 1


def test_history_stays_bounded(tmp_path, monkeypatch):
    """Unbounded append on a device with a small SD card is a slow way to
    take the whole thing down."""
    monkeypatch.setattr(dsn_settings, "HISTORY_PATH", tmp_path / "h.jsonl")
    monkeypatch.setattr(dsn_history, "HISTORY_MAX_BYTES", 2000)
    monkeypatch.setattr(dsn_history, "HISTORY_MAX_LINES", 10)
    for i in range(400):
        dsn_history.append_history([{"t": float(i), "event": "appear",
                             "dish": "DSS43", "craft": f"C{i}", "flags": []}])
    path = tmp_path / "h.jsonl"
    # The property is BYTES, not an exact line count: the trim fires when the
    # ceiling is crossed, so the file rides just above the trimmed size
    # between trims. What must never happen is unbounded growth.
    assert path.stat().st_size < dsn_history.HISTORY_MAX_BYTES * 2
    lines = path.read_text().splitlines()
    # and it keeps the NEWEST, not the oldest
    assert json.loads(lines[-1])["craft"] == "C399"
    assert len(lines) < 400, "nothing was trimmed at all"


def test_the_history_cap_leaves_headroom():
    """Sizing the line cap right at the byte ceiling makes every single append
    rewrite the whole file. The trim has to land well below the trigger."""
    after_trim = dsn_history.HISTORY_MAX_LINES * 110      # generous bytes per line
    assert after_trim < dsn_history.HISTORY_MAX_BYTES * 0.75, (
        "a trim would immediately re-trigger; widen the gap")


def test_a_missing_history_file_is_a_cold_start_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(dsn_settings, "HISTORY_PATH", tmp_path / "nope.jsonl")
    state = dsn_model.State()
    dsn_history.load_history(state)
    assert state.seen == {}
