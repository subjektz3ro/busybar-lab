"""Skystrip render / backroads."""

from __future__ import annotations

import math
import random

from apps.skystrip_app import limits as _limits
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import grove as _render_grove
from apps.skystrip_app.render import precipitation as _render_precipitation
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import season as _render_season
from apps.skystrip_app.render import vegetation as _render_vegetation


def lights_on_train(elev: float, wx) -> bool:
    return elev < 2 or wx.stormy


def _road_R(x: int) -> int:
    """Side-view road line: dead level.

    Every amplitude tried (1.0, then 0.8) rounded into row-steps that a
    context-free review read as disconnected stripes "wrapping around the
    display like text". On 16 rows a two-lane reads as a road when it is
    a line; the poles, dashes, and traffic carry the depth."""
    return 11


# The lit conifer on the shoulder. _road_R(x) is a rolling profile present
# at every column -- there's no off-road column span to sit in, only a row
# that varies with x. x=58..71 is the widest stretch where _road_R holds
# steady at its lowest value (10, giving the most clearance below it), and
# it's clear of the poplar lane (trunks at BACKROADS_POPLARS). (64, 15)
# centers the tree in that stretch, with its whole footprint (rows 12-15)
# sitting below _road_R(x)+1 -- the road and its painted shoulder line --
# for every column the tree actually touches (63-65).
BACKROADS_TREE = (64, 15)

# The poplar lane: five tall flames, evenly spaced, the road running
# behind their trunks — the operator's lookbook pick (option B, then
# tree design G, 2026-08-12). x=21 keeps the first crown east of the
# status corner; x=61 keeps the last clear of the lit conifer above.
BACKROADS_POPLARS = (21, 31, 41, 51, 61)


def _draw_backroads(
    px,
    local,
    elev: float,
    daylight: float,
    seed: int,
    phase: float,
    wx,
    amb: tuple,
    moon_ill: float,
    cloud: float,
    lane: bool = True,
) -> None:
    """Eye-level americana: a two-lane road through rolling farmland.
    Telephone poles carry sagging wire, a farmhouse sits on the far
    hill, and side-profile cars cruise both ways, strobing behind the
    trunks of a five-poplar lane and showing back up. The sky above is
    the living sky, same as every side-view scene."""
    # Snapshot the living sky before this scene paints over it -- same
    # reasoning as _draw_grove's sky_before: the gradient is noisy, so
    # there's no fixed palette to match, only what was here a moment ago.
    sky_before = {px[x, y] for x in range(_limits.W) for y in range(6, 16)}
    mm = local.month
    fall = mm in (9, 10, 11)
    winter = _render_grove.is_winter(local)
    dl = max(0.22, daylight)  # built structures keep a face at night
    dl_f = max(0.06, daylight)  # the land itself goes honestly dark

    if winter:
        far_n, far_d = (40, 44, 56), (152, 158, 170)
        fld_n, fld_d = (48, 52, 64), (198, 204, 214)
    elif fall:
        far_n, far_d = (38, 32, 16), (124, 100, 48)
        fld_n, fld_d = (46, 38, 16), (164, 132, 52)
    else:
        far_n, far_d = (18, 30, 18), (58, 88, 46)
        fld_n, fld_d = (24, 40, 22), (82, 120, 54)
    far_c = _render_primitives._shade(
        _render_primitives._lerp_rgb(far_n, far_d, dl_f), amb
    )
    fld_c = _render_primitives._shade(
        _render_primitives._lerp_rgb(fld_n, fld_d, dl_f), amb
    )

    # Far hills roll along the horizon; fields run down to the road
    for x in range(_limits.W):
        hill = 8 + round(1.4 * math.sin(x * 0.05 + 0.5))
        R = _road_R(x)
        for y in range(hill, min(R, _limits.H)):
            px[x, y] = far_c if y < hill + 2 else fld_c
        for y in range(R + 2, _limits.H):  # near verge below the road
            px[x, y] = fld_c

    # The rail line rides a distant ridge along the horizon, east of the
    # clock corner. A crossing train (watch_trains' one-shot overlay)
    # enters at one screen edge and leaves at the other; on the west side
    # the only thing it passes behind is the clock card itself, which
    # touches the frame edge — no invented structure needed. Two earlier
    # versions taught that lesson: a trestle on stilts read as a fence
    # floating in the sky, and a 3-pixel "grain elevator" (then a whole
    # knob-and-silo complex) read as a magician's cabinet eating freights.
    ridge_c = _render_primitives._shade(
        _render_primitives._lerp_rgb(
            _render_primitives._rgb_int(v * 0.55 for v in far_n),
            _render_primitives._rgb_int(v * 0.7 for v in far_d),
            dl_f,
        ),
        amb,
    )
    # Full width — starting the ridge at the corner's edge once left a
    # sky-colored slab and a four-row horizon step there (blind review,
    # round two). Red ink is hue-separated from the olive crest, so the
    # crest no longer needs to duck the status corner.
    for x in range(_limits.W):
        hill = 8 + round(1.4 * math.sin(x * 0.05 + 0.5))
        for y in range(6, hill):
            px[x, y] = ridge_c
    # Rail bed: one subtle row along the ridge crest, from the status
    # card's fixed edge (x=20) to the screen edge — continuous under the
    # whole train band, so a crossing freight is on rail for every visible
    # column of its journey.
    # Steel, not a brightness whisper: the first version lifted the ridge
    # tone by ~10%, under the 30% floor, and the panel showed no track at
    # all — the operator asked where the rail line went. Cool grey against
    # the olive ridge, day and night.
    # Night floor eased to 0.3: at 0.4 a blind review read the unlit
    # night rail as "a broken underline attached to the clock" floating
    # over an invisible ridge. Steel by day, a whisper by night —
    # headlights own that hour anyway.
    rail_c = _render_primitives._shade(
        _render_primitives._lerp_rgb((70, 72, 82), (150, 152, 162), max(0.3, daylight)),
        amb,
    )
    for x in range(_limits.W):
        px[x, 5] = rail_c  # hidden beneath the card west of STATUS_CARD_W

    # Farmhouse on the far hill: walls, roof, a window that keeps watch.
    # East of center — its old spot (x=9) hid it under the clock corner.
    hx = 46
    # White clapboard, dark roof: a brown-on-brown 3x3 block was
    # "genuinely unidentifiable" to a viewer who didn't know it was there.
    wall = _render_primitives._shade(
        _render_primitives._lerp_rgb((46, 44, 40), (206, 200, 188), dl), amb
    )
    roof = _render_primitives._shade(
        _render_primitives._lerp_rgb((44, 16, 12), (128, 46, 36), dl), amb
    )

    # The barn, left of centre and across the road from the house. Placement
    # is deliberate: the scene's structures live in the gaps between poplars,
    # the clock owns columns 0-18, and putting both buildings in the right
    # half left the whole left of the road empty. Red against the fields is
    # also the scene's only strong hue — it measured last of six for colour.
    bx0, bx1 = 23, 31
    barn_body = _render_primitives._shade(
        _render_primitives._lerp_rgb((44, 14, 10), (176, 48, 34), dl), amb
    )
    barn_roof = _render_primitives._shade(
        _render_primitives._lerp_rgb((30, 29, 28), (138, 132, 122), dl), amb
    )
    for x in range(bx0 + 1, bx1):
        px[x, 6] = barn_roof
    for x in range(bx0, bx1 + 1):
        px[x, 7] = barn_roof
    for x in range(bx0, bx1 + 1):
        for y in range(8, 11):
            px[x, y] = barn_body
    # The white X-brace door: the mark that says BARN at any distance.
    door = _render_primitives._rgb_int(v * 0.45 for v in barn_body)
    trim = _render_primitives._shade(
        _render_primitives._lerp_rgb((70, 66, 58), (216, 206, 184), dl), amb
    )
    for x in range(bx0 + 3, bx0 + 7):
        for y in range(9, 11):
            px[x, y] = door
    for tx2, ty2 in ((bx0 + 3, 9), (bx0 + 6, 9), (bx0 + 4, 10), (bx0 + 5, 10)):
        px[tx2, ty2] = trim
    if elev < 2 or wx.stormy:  # the yard lamp, after dark
        px[bx0 + 1, 8] = (255, 190, 90)
    # A five-wide roof with eaves over three-wide walls, plus a chimney:
    # at 3x3 the blind reviews called it "mushroom, tent, buoy, person".
    # The overhang is what says "house".
    for dx in range(-2, 3):
        px[hx + dx, 7] = roof
    for dx in range(-1, 2):
        px[hx + dx, 8] = wall
        px[hx + dx, 9] = wall
    px[hx + 1, 6] = _render_primitives._rgb_int(v * 0.6 for v in roof)
    px[hx + 2, 6] = _render_primitives._rgb_int(v * 0.6 for v in roof)  # chimney
    if elev < 2 or wx.stormy:
        # Two lit windows and a porch lamp. One pixel of warm light was all
        # this scene had at night, against the other scenes' many.
        px[hx, 8] = (255, 190, 90)
        px[hx - 1, 8] = (255, 206, 128)
        px[hx + 1, 9] = _render_primitives._rgb_int(v * 0.72 for v in (255, 196, 96))
    # Woodsmoke when it is cold enough to have the stove going. Three puffs
    # on whole cycles, so the loop seam never jumps.
    if wx.temp_c < 8.0:
        for i in range(3):
            drift = math.sin(math.tau * (phase + i / 3.0))
            sx = hx + 2 + int(round(drift * 1.4))
            sy = 5 - i
            if 0 <= sx < _limits.W and 0 <= sy < _limits.H:
                px[sx, sy] = _render_primitives._shade(
                    _render_primitives._rgb_int(
                        v * (0.75 - i * 0.2) for v in (168, 168, 174)
                    ),
                    amb,
                )

    # The road: surface line + shoulder, dashes suggesting markings.
    # Dimmer floor than the buildings: at night the road is where the
    # headlights are, not a lit ribbon of its own.
    asphalt = _render_primitives._shade(
        _render_primitives._lerp_rgb(
            (32, 32, 36), (108, 108, 114), max(0.15, daylight)
        ),
        amb,
    )
    shoulder = tuple(int(v * 0.72) for v in asphalt)
    dash_c = _render_primitives._shade(
        _render_primitives._lerp_rgb((92, 92, 88), (215, 215, 205), dl), amb
    )
    moonf = moon_ill * max(0.0, 1.0 - cloud * 1.2)
    lift = int(12 * moonf)
    for x in range(_limits.W):
        R = _road_R(x)
        px[x, R] = tuple(min(255, v + lift) for v in asphalt)
        if R + 1 < _limits.H:
            px[x, R + 1] = shoulder
        # Three lit, ten dark. The road is one pixel tall, so a dash does
        # not sit ON the asphalt, it REPLACES it — at the old two-on/two-off
        # the row came out 34 dark pixels alternating with 33 near-white
        # ones, which on a gapped panel is speckle rather than a road. Sparse
        # bright marks on a dark ribbon is both the real thing and the way
        # this display wants to be drawn.
        if x % 13 < 3:
            px[x, R] = (
                tuple(min(255, v + lift) for v in dash_c)
                if daylight > 0.3
                else px[x, R]
            )

    # The far fields carry static texture, not motion, and that is a
    # measured decision rather than a concession. The field was one flat
    # colour ("no shading or variance to the grass colour isn't helping"),
    # so it gets shading — but a wind wave does not work in two rows. With
    # the gust at full strength, 16 of 72 pixels per row changed by more
    # than the panel's ~30% visibility floor between gust phases, and a
    # viewer shown two phases blind still called them the same picture.
    # Scattered intensity has no shape to follow; the eye tracks edges and
    # objects. So the wind lives where this scene has both contrast and
    # shape — the poplar crowns against bright sky, and the verge grass,
    # both of which change SILHOUETTE.
    #
    # Rows 9-11 are also the traffic overlay's band, and it composites a
    # snapshot of them; motion here would freeze whenever a car passed.
    field_rows = tuple(y for y in range(8, _road_R(0)) if y >= 0)
    for y in field_rows:
        for x in range(_limits.W):
            base_c = px[x, y]
            if base_c == ridge_c:  # the ridge is rock, not crop
                continue
            px[x, y] = _render_primitives._rgb_int(
                v * _render_vegetation.field_mottle(x, y - field_rows[0])
                for v in base_c
            )

    # One wind direction for everything that bends in this scene: the
    # verge grass below and the poplar crowns above.
    lane_lean = 0
    if wx.wind_dir is not None and wx.wind_kmh >= 5:
        comp = math.sin(math.radians(wx.wind_dir + 180))
        lane_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)

    verge_rows = (_road_R(0) + 2, _road_R(0) + 3, _road_R(0) + 4)
    top_row, mid_row, base_row = verge_rows
    # Wind is a change of SHAPE, not of brightness. The panel crushes a
    # luminance ripple (under ~30% it is invisible), so the swell changes
    # how tall each blade stands: the grass line rises and falls as the gust
    # travels, and the tallest tips lean with it. One front per loop, which
    # both edges of the panel are clear of at the seam.
    shade_c = _render_primitives._rgb_int(v * 0.45 for v in px[0, top_row])
    blade_c = _render_primitives._shade(
        _render_primitives._lerp_rgb((26, 34, 22), (74, 104, 48), daylight), amb
    )
    tip_c = _render_primitives._shade(
        _render_primitives._lerp_rgb((32, 42, 26), (96, 132, 60), daylight), amb
    )
    # The mass of the verge is the bottom row; the tufts stand on it, and
    # the row under the shoulder is shadow the tufts read against.
    for x in range(_limits.W):
        if top_row < _limits.H:
            px[x, top_row] = shade_c
        if mid_row < _limits.H:
            px[x, mid_row] = shade_c
        if base_row < _limits.H:
            px[x, base_row] = blade_c
    downwind = lane_lean or 1
    steady = min(1.0, wx.wind_kmh / 30.0)
    for x in range(_limits.W):
        gust = _render_vegetation.verge_gust(x, phase)
        press = (steady * 0.42 + gust * 0.85) * _render_vegetation.VERGE_FLEX[x]
        height = _render_vegetation.VERGE_BLADES[x] + (1 if press > 0.46 else 0)
        lean = downwind if press > 0.62 else 0
        for i in range(height):
            y = base_row - i
            if not top_row <= y < _limits.H:
                continue
            tip = i == height - 1
            xx = x + lean if tip else x
            if not 0 <= xx < _limits.W:
                continue
            base_c = tip_c if tip else blade_c
            # Tone: this column's own green, lifted where the gust has the
            # blades turned over, times the untrackable per-pixel shimmer.
            f = (
                _render_vegetation.VERGE_TONE[x]
                * (1.0 + 0.16 * press)
                * _render_vegetation.verge_shimmer(
                    xx, y - top_row, phase, wx.wind_kmh, gust
                )
            )
            px[xx, y] = tuple(max(0, min(255, int(v * f))) for v in base_c)

    if (
        elev < -2
        and local.month in (6, 7, 8)
        and wx.temp_c >= 15.0
        and not (wx.rain or wx.snow or wx.stormy)
    ):
        fly_rng = random.Random(seed * 61 + 7)
        for _ in range(6):
            fx = fly_rng.randrange(2, _limits.W - 2)
            fy = fly_rng.choice(verge_rows)
            blink = math.sin(math.tau * (2 * phase + fly_rng.random()))
            if blink > 0.15 and fy < _limits.H:
                px[fx, fy] = _render_primitives._rgb_int(
                    v * (0.45 + 0.55 * blink) for v in _render_vegetation.FIREFLY_COLOR
                )

    # The poplar lane: five tall flames the road runs behind — the
    # operator's pick from the lookbook rounds (lane, then poplars).
    # Drawn after the cars, so traffic strobes behind five trunks per
    # crossing. Each tree is seeded to its own height and, in autumn,
    # its own hue; winter bares them to twig spires; the top two rows
    # sway on whole gust cycles so the loop seam never jumps.
    # `lane=False` renders the same road with no poplars. The one-shot
    # freight overlay diffs the two to learn which sky-band pixels are
    # foreground trees, so a passing boxcar never slices a crown.
    p_trunk = _render_primitives._shade(
        _render_primitives._lerp_rgb(
            (40, 31, 24),
            _render_primitives._rgb_int(
                min(255, v * 1.3) for v in _render_vegetation.TRUNK_DAY
            ),
            dl,
        ),
        amb,
    )
    # The crown lives ABOVE the traffic band; only the trunk crosses it.
    # The first lane put the crown at rows 5-11 — down through the road —
    # five pixels wide, so foliage owned 25 of the road's 52 columns and a
    # car vanished completely between trees, then reappeared on the far
    # side ("cars that spawn from trees or disappear into trees", from the
    # panel, 2026-08-15). A tree hides traffic with its trunk.
    crown_bottom = _road_R(0) - 3
    for pi, txp in enumerate(BACKROADS_POPLARS if lane else ()):
        p_rng = random.Random(97 * pi + 5)
        top = 3 + p_rng.randrange(2)
        # A single-pixel trunk, centred: the doubled trunk read heavy
        # from the physical panel ("the thickness of the trunk throws
        # off the trees"), and a full-height vertical line survives the
        # LED gaps where an isolated single pixel would not. It runs from
        # under the crown to the ground, crossing field, road, and verge,
        # so a passing car is interrupted by exactly one pixel.
        for y in range(crown_bottom + 1, 16):
            px[txp, y] = p_trunk
        if winter:
            # A bare poplar is a twig spire: the trunk keeps rising,
            # with alternating stub branches for texture.
            for y in range(top, crown_bottom + 1):
                px[txp, y] = p_trunk
                stub = _render_primitives._rgb_int(v * 0.8 for v in p_trunk)
                if y % 2 == pi % 2 and txp - 1 >= 0:
                    px[txp - 1, y] = stub
                elif txp + 1 < _limits.W:
                    px[txp + 1, y] = stub
            continue
        if fall:
            p_day = _render_art.GROVE_FALL[p_rng.randrange(len(_render_art.GROVE_FALL))]
            p_night = _render_primitives._rgb_int(v * 0.30 for v in p_day)
        else:
            p_day, p_night = (58, 118, 44), (16, 27, 15)
        # The same gust front the grass and the crop read. Each tree used
        # its own phase offset (`+ txp` radians, effectively random per
        # tree), so the five of them fidgeted independently and the wind
        # never read as one event crossing the scene. A poplar against
        # bright sky is the highest-contrast thing here and the way people
        # actually SEE wind, so this is where the motion has to land — the
        # far field measured 16 of 72 pixels over the visibility floor
        # between gust phases and a blind viewer still called two phases
        # the same picture, because scattered intensity has no shape to
        # follow.
        sway = 0.0
        if wx.wind_kmh >= 8:
            sway = _render_vegetation.verge_gust(txp, phase) * (lane_lean or 1)
        for y in range(top, crown_bottom + 1):
            # Odd widths, symmetric about the trunk: 3 wide at the tip
            # and base of the crown, 5 wide through the middle.
            w_row = 1 if y in (top, crown_bottom) else 2
            # Bend tapers to the tip: the crown's base barely moves, the
            # top two rows carry up to two pixels. A whole tree sliding
            # sideways reads as a glitch; a tree bending reads as wind.
            depth = (crown_bottom - y) / max(1, crown_bottom - top)
            row_sway = int(round(sway * 2.0 * depth * depth))
            for dx in range(-w_row, w_row + 1):
                x2 = txp + dx + row_sway
                if not 0 <= x2 < _limits.W:
                    continue
                lit = (0.6 if p_rng.random() < 0.25 else 1.0) * max(0.25, daylight)
                c = _render_primitives._rgb_int(
                    _render_primitives._lerp_rgb(p_night, p_day, lit)
                )
                if y == top and daylight > 0.4:
                    c = _render_primitives._rgb_int(min(255, v * 1.35) for v in c)
                px[x2, y] = _render_primitives._shade(c, amb)

    # Settled snow: everything below the sky takes it on the top edge,
    # EXCEPT the road itself -- a ploughed road reads wet-black, and white
    # shoulders against it is the honest picture. asphalt/dash_c/shoulder
    # are exactly the colours this scene just used to paint the road, so
    # excluding them (same mechanism as sky_before) keeps snow off the
    # pavement without inventing a new predicate for where the road is.
    # The road is level now, so there are no softened step seams to
    # exclude alongside them.
    tier = _render_precipitation.snow_tier(wx.snow_depth_m)
    if tier:
        road_colors = {
            tuple(min(255, v + lift) for v in asphalt),
            tuple(min(255, v + lift) for v in dash_c),
            shoulder,
        }
        snow_tops = _render_precipitation.surface_tops(
            px, range(_limits.W), range(6, 16), sky_before | road_colors
        )
        _render_precipitation.settle_snow(px, snow_tops, tier, amb)

    # A tree on the shoulder, drawn dead last so nothing painted above --
    # settled snow included -- lands on top of it. BACKROADS_TREE was
    # chosen against _road_R itself (see its comment) so the road stays
    # clear without a fixed column range, the same problem the
    # settled-snow block above solves by excluding the road's own live
    # colours instead of a column span.
    if _render_season.is_christmas(local):
        tx, ty = BACKROADS_TREE
        _render_season.draw_lit_tree(px, tx, ty, phase, amb)
