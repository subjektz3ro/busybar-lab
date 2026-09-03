"""Text-to-speech onto the bar (no ffmpeg needed on either platform).

The bar plays ``.snd`` files: headerless raw PCM, s16le, mono, 44100 Hz.
Kokoro is required for supported Linux production; ``SKYSTRIP_VOICE`` selects
a voice from its shared model bank (default ``am_michael``), and installation
or deployment fails if real synthesis does not work. macOS development uses
``say``. Linux retains ``espeak-ng`` only as emergency resilience after a
verified installation. All paths end in the same mono/44100/gain normalization.
"""

from __future__ import annotations

import array
import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from busylib import BusyBar

from .device import ASSET_ROOT, asset_path, content_asset_name

DEFAULT_VOICE = "Karen"
DEFAULT_KOKORO_VOICE = "am_michael"

logger = logging.getLogger("busybar_dev.tts")


def tts_speed() -> float:
    """``SKYSTRIP_TTS_SPEED`` as a float, warning instead of crashing.

    This runs inside the synth thread, where a raised ValueError reads as a
    Kokoro bug and silently kills the spoken report; a warned fallback keeps
    the voice working while naming the actual mistake.
    """
    raw = (os.environ.get("SKYSTRIP_TTS_SPEED") or "").strip()
    if not raw:
        return 1.0
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "SKYSTRIP_TTS_SPEED=%r is not a number; speaking at 1.0", raw)
        return 1.0
TARGET_PEAK = 29000  # near full-scale for s16; the bar's speaker is quiet
KOKORO_MODEL_SIZE = 325_532_387
KOKORO_BANK_SIZE = 28_214_398
DEFAULT_KOKORO_DIR = Path(__file__).resolve().parent.parent / "voices"


_KOKORO_RE = re.compile(r"[a-z]{2}_[a-z_]+")
_KOKORO = None  # loaded once; the model takes a few seconds to open


def resolve_kokoro_dir(configured: str | Path | None) -> Path:
    """Resolve a configured model directory against the checkout root."""
    candidate = Path(configured) if configured else DEFAULT_KOKORO_DIR
    if not candidate.is_absolute():
        candidate = DEFAULT_KOKORO_DIR.parent / candidate
    return candidate.resolve()


def configured_kokoro_dir(env_path: Path | None = None) -> Path:
    """Return the model directory the installed service will read from .env.

    Installer and deploy processes may carry unrelated exported variables,
    while the systemd service starts clean and loads the checkout's ``.env``.
    Reading that file directly keeps model management and service runtime on
    the same bank.
    """
    from .config import ENV_PATH, read_env_file

    values = read_env_file(
        ENV_PATH if env_path is None else env_path,
        strip_quotes=True,
    )
    return resolve_kokoro_dir(values.get("SKYSTRIP_VOICE_DIR"))


def _kokoro_paths() -> tuple[Path, Path] | None:
    # Barkeep intentionally stores an explicit empty string when an operator
    # clears an override.  ``Path("")`` means the current working directory,
    # not "use the default", so a blank value used to make service and CLI
    # launches search different places for the same model bank.
    vdir = resolve_kokoro_dir(os.environ.get("SKYSTRIP_VOICE_DIR"))
    model, bank = vdir / "kokoro-v1.0.onnx", vdir / "voices-v1.0.bin"
    try:
        complete = (
            model.is_file()
            and model.stat().st_size == KOKORO_MODEL_SIZE
            and bank.is_file()
            and bank.stat().st_size == KOKORO_BANK_SIZE
        )
    except OSError:
        complete = False
    return (model, bank) if complete else None


def _kokoro_importable() -> bool:
    """Whether the neural engine imports in this interpreter.

    Model files outlive virtual environments, so a damaged or only partly
    restored environment can retain ``voices/`` without the Python package.
    Runtime keeps the system engine as emergency resilience; install.sh is
    stricter and refuses a production Linux setup that cannot use Kokoro.
    """
    try:
        from kokoro_onnx import Kokoro as _Kokoro  # noqa: F401
    except Exception:  # noqa: BLE001 - binary/import incompatibility is absence
        return False
    return True


def tts_engine_status() -> tuple[str | None, str]:
    """Return the engine runtime would select and a truthful operator summary."""
    model_paths = _kokoro_paths()
    if model_paths is not None and _kokoro_importable():
        return (
            "kokoro",
            "Kokoro selected: its Python engine imports and both model files "
            "have the expected sizes.",
        )

    retained_models = model_paths is not None
    if shutil.which("say") and shutil.which("afconvert"):
        detail = "macOS say selected."
        if retained_models:
            detail += (
                " Retained Kokoro models were ignored because its "
                "Python engine is unavailable."
            )
        return "say", detail
    if shutil.which("espeak-ng"):
        detail = "espeak-ng fallback selected."
        if retained_models:
            detail += (
                " Retained Kokoro models were ignored because its "
                "Python engine is unavailable."
            )
        return "espeak-ng", detail

    detail = (
        "No usable speech engine was found; spoken narration is unavailable."
    )
    if retained_models:
        detail += (
            " Retained Kokoro models cannot be used without its Python engine."
        )
    return None, detail


def _kokoro_synth(text: str, voice: str) -> tuple[bytes, int]:
    """Kokoro neural TTS: best prosody of the local engines."""
    global _KOKORO
    if _KOKORO is None:
        from kokoro_onnx import Kokoro
        paths = _kokoro_paths()
        if paths is None:
            # Callers check _kokoro_paths() before choosing this engine, but
            # that check and this load are two separate stats: a half-written
            # or deleted voice bank between them used to raise "cannot unpack
            # non-sequence NoneType" from inside the synth thread, where it
            # reads as a Kokoro bug rather than a missing model.
            raise RuntimeError(
                "Kokoro model files are missing or incomplete; "
                "re-run deploy/install.sh to fetch the voice bank")
        model, bank = paths
        _KOKORO = Kokoro(str(model), str(bank))
    samples, rate = _KOKORO.create(
        text, voice=voice, speed=tts_speed(), lang="en-us")
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return pcm, rate


def _kokoro_voice(requested: str) -> str:
    """Map obsolete/non-Kokoro configuration to the supported default.

    Existing hosts keep their gitignored ``.env`` across upgrades.  Once the
    shared model bank is resident, an older engine's identifier must not
    strand them on the system fallback forever.
    """
    return requested if _KOKORO_RE.fullmatch(requested) else DEFAULT_KOKORO_VOICE


def _say_voice(requested: str) -> str:
    """Keep an explicit macOS voice, but never pass a Kokoro id to ``say``."""
    return DEFAULT_VOICE if _KOKORO_RE.fullmatch(requested) else requested


_ONES = ("zero one two three four five six seven eight nine ten eleven "
         "twelve thirteen fourteen fifteen sixteen seventeen eighteen "
         "nineteen").split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty",
         6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def _num_words(n: int) -> str:
    if n < 0:
        return "minus " + _num_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, r = divmod(n, 10)
        return _TENS[t] + ("" if not r else " " + _ONES[r])
    if n < 1000:
        h, r = divmod(n, 100)
        return _ONES[h] + " hundred" + ("" if not r else " " + _num_words(r))
    return str(n)  # past our needs; let the engine cope


def _decimal_words(text: str) -> str:
    """Speak a decimal instead of letting its point become a full stop.

    ``-?\\d+`` used to match "56" and "0" separately in "56.0", leaving the
    point behind: the engine heard "fifty six." and started a new sentence at
    "zero percent". A trailing ".0" is dropped because it is noise from a
    float, not a measurement anyone says aloud.
    """
    whole, dot, frac = text.partition(".")
    words = _num_words(int(whole))
    frac = frac.rstrip("0")
    if not dot or not frac:
        return words
    return words + " point " + " ".join(_ONES[int(digit)] for digit in frac)


def _time_words(h: int, m: int) -> str:
    if m == 0:
        return _num_words(h) + " o'clock"
    if m < 10:
        return f"{_num_words(h)} oh {_num_words(m)}"
    return f"{_num_words(h)} {_num_words(m)}"


def speakable(text: str) -> str:
    """Rewrite text the way a narrator would read it aloud.

    Neural voices are trained on prose: digits, clock times, and
    em-dashes make them stumble. '5:48' -> 'five forty eight',
    '74' -> 'seventy four', '56.0' -> 'fifty six', dashes -> commas.
    """
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b",
                  lambda m: _time_words(int(m.group(1)), int(m.group(2))),
                  text)
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(
        r"-?\d+(?:\.\d+)?",
        lambda match: _decimal_words(match.group()),
        text,
    )
    return text


def synth_snd(text: str, voice: str | None = None) -> bytes:
    """Render ``text`` to bar-ready raw PCM bytes.

    A supported explicit ``voice`` wins over SKYSTRIP_VOICE, which is only the
    default for callers that don't name one. A non-Kokoro value is normalized
    to the supported default whenever the shared model is resident. The
    explicit argument used to be discarded entirely: dsn asked for af_nova
    and got Skystrip's am_michael.
    """
    from busybar_dev import load_env
    load_env()  # engine choice can read SKYSTRIP_VOICE — fold .env in FIRST
    text = speakable(text)
    requested_voice = (
        voice or os.environ.get("SKYSTRIP_VOICE") or DEFAULT_KOKORO_VOICE)
    engine, _detail = tts_engine_status()
    if engine == "kokoro":
        kvoice = _kokoro_voice(requested_voice)
        frames, rate = _kokoro_synth(text, kvoice)
        channels = 1
        return _normalize(frames, rate, channels)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "tts.wav"
        if engine == "say":  # macOS
            aiff = Path(tmp) / "tts.aiff"
            # A Kokoro identifier is not a macOS voice name.  If its model is
            # absent, use the known system default; an explicitly configured
            # non-Kokoro name remains a valid way to select a `say` voice.
            subprocess.run(["say", "-v", _say_voice(requested_voice),
                            "-o", str(aiff), text],
                           check=True)
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@44100", "-c", "1",
                 str(aiff), str(wav)],
                check=True,
            )
        elif engine == "espeak-ng":  # the Pi
            subprocess.run(
                ["espeak-ng", "-v", "en-us", "-s", "160",
                 "-w", str(wav), text],
                check=True,
            )
        else:
            raise RuntimeError(
                "no TTS engine: need Kokoro model files, macOS `say`, "
                "or `espeak-ng` on Linux")
        with wave.open(str(wav), "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            frames = wf.readframes(wf.getnframes())
    return _normalize(frames, rate, channels)


def verify_kokoro_synthesis(model_dir: str | Path | None = None) -> str:
    """Fail unless the required neural path produces real bar-ready audio.

    A package import and correctly sized model files are necessary but not
    sufficient: native-runtime failures can appear only when inference runs.
    The installer and deployer both use this check before reporting a healthy
    production release.
    """
    from busybar_dev import load_env

    # Engine selection must happen after .env is folded in. Otherwise a
    # custom/blank model directory could change the second selection inside
    # synth_snd and let espeak audio satisfy a nominal Kokoro check.
    load_env()
    if model_dir is not None:
        os.environ["SKYSTRIP_VOICE_DIR"] = str(resolve_kokoro_dir(model_dir))
    engine, detail = tts_engine_status()
    if engine != "kokoro":
        raise RuntimeError(f"required Kokoro engine is unavailable: {detail}")
    try:
        frames, rate = _kokoro_synth(
            speakable("BUSY Bar speech check."), DEFAULT_KOKORO_VOICE
        )
        if rate <= 0 or len(frames) < max(2, rate // 5) or len(frames) % 2:
            raise RuntimeError(
                "Kokoro returned invalid raw mono PCM "
                f"({len(frames)} bytes at {rate} Hz)"
            )
        raw_samples = [
            int.from_bytes(
                frames[offset:offset + 2], "little", signed=True
            )
            for offset in range(0, len(frames), 2)
        ]
        raw_peak = max(abs(sample) for sample in raw_samples)
        raw_range = max(raw_samples) - min(raw_samples)
        active_floor = max(2, raw_peak // 20)
        active_samples = sum(
            abs(sample) >= active_floor for sample in raw_samples
        )
        if (
            raw_peak < 100
            or raw_range < 200
            or active_samples < max(20, len(raw_samples) // 100)
        ):
            raise RuntimeError(
                "Kokoro returned silent or flat raw samples "
                f"(peak {raw_peak}, range {raw_range}, "
                f"active {active_samples})"
            )
        pcm = _normalize(frames, rate, 1)
    except Exception as exc:
        raise RuntimeError(
            f"Kokoro could not synthesize real audio: {exc}"
        ) from exc
    if len(pcm) < 8_820 or len(pcm) % 2:
        raise RuntimeError(
            f"Kokoro returned invalid 44.1 kHz mono PCM ({len(pcm)} bytes)"
        )
    return (
        "Kokoro verified with a real synthesis "
        f"({len(pcm):,} PCM bytes, raw peak {raw_peak:,})."
    )


def _settle_synthesis(
    future: asyncio.Future,
    error: BaseException | None,
    value: bytes | None,
) -> None:
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)


async def synth_snd_async(text: str, voice: str | None = None) -> bytes:
    """Run synthesis off-loop without a non-daemon executor blocking exit."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def work() -> None:
        try:
            value, error = synth_snd(text, voice), None
        except BaseException as exc:  # noqa: BLE001 - delivered to awaiter
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(_settle_synthesis, future, error, value)
        except RuntimeError:
            pass  # event loop closed during process shutdown

    threading.Thread(
        target=work,
        daemon=True,
        name="busybar-tts",
    ).start()
    return await future


def _normalize(frames: bytes, rate: int, channels: int) -> bytes:
    """Shared tail: any engine's PCM -> bar-ready mono 44100 s16."""
    if channels == 2:  # take the left channel
        mono = array.array("h", frames)
        frames = mono[0::2].tobytes()
    if rate != 44100:  # nearest-sample resample is fine for speech
        src = array.array("h", frames)
        n_out = int(len(src) * 44100 / rate)
        frames = array.array(
            "h", (src[min(len(src) - 1, int(i * rate / 44100))]
                  for i in range(n_out))).tobytes()
    samples = array.array("h", frames)
    peak = max(1, max(samples, default=1), -min(samples, default=-1))
    if peak < TARGET_PEAK:
        gain = TARGET_PEAK / peak
        samples = array.array(
            "h", (max(-32768, min(32767, int(s * gain))) for s in samples)
        )
    return samples.tobytes()


# How many generated clips to leave on the device per application_name.
SAY_CACHE_KEEP = 4


def say_asset_name(pcm: bytes) -> str:
    """The device path for one synthesized clip, derived from its own bytes."""
    return content_asset_name("tts_", pcm, suffix=".snd")


def _reap_say_cache(bb: BusyBar, app_name: str, keep: str) -> None:
    """Bound the generated clips, and reclaim what earlier versions stranded.

    Nothing swept `tts_*` before: the name carried a timestamp modulo, the
    in-memory record of "what I uploaded" died with the process, and neither
    app's sweep pattern matched the prefix. Two orphans totalling 2 MB were
    found resident on a live device, alongside a bare `tts.snd` from an even
    older naming scheme.
    """
    try:
        entries = bb.storage_list(f"{ASSET_ROOT}/{app_name}").list
    except Exception:  # noqa: BLE001 - reaping is best-effort, never fatal
        return
    ours = [e.name for e in entries
            if e.name.startswith("tts") and e.name.endswith(".snd")
            and e.name != keep]
    # Content-addressed names carry no order, so keep an arbitrary but bounded
    # subset rather than pretending to know which is newest.
    for name in sorted(ours)[: max(0, len(ours) - (SAY_CACHE_KEEP - 1))]:
        try:
            bb.storage_remove(asset_path(app_name, name))
        except Exception:  # noqa: BLE001
            pass


def say_on_bar(
    bb: BusyBar,
    text: str,
    *,
    voice: str | None = None,
    app_name: str = "tts",
    filename: str | None = None,
) -> None:
    """Synthesize ``text``, upload it under ``app_name``, and play it.

    The filename is a digest of the audio itself. The bar caches assets by
    path forever, which is a trap for a mutable name and the mechanism for an
    immutable one: identical bytes mean an identical path, so re-saying the
    same line re-uploads nothing, and no path ever receives different bytes —
    so neither the stale-blocks case nor the 508 "file is open" case can fire.

    It used to be ``tts_{int(time.time()) % 1000000}.snd``. Two calls in the
    same second collided outright, and the modulo wraps; the skill names both.
    """
    pcm = synth_snd(text, voice)
    if filename is None:
        filename = say_asset_name(pcm)
    bb.assets_upload(app_name, filename, pcm)
    bb.audio_play(path=filename, application_name=app_name)
    _reap_say_cache(bb, app_name, keep=filename)
