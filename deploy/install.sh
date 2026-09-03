#!/usr/bin/env bash
# Installer — from a fresh clone to a running bar.
#
# Sets up the Python environment, writes a .env, installs and verifies the
# production Linux speech stack, and can install barkeep as the one systemd
# service that supervises every app.
# Production install: 64-bit glibc 2.28+ Linux on x86_64/aarch64 (including
# 64-bit Raspberry Pi OS). A direct-run development path also works on macOS.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_DIR=$(pwd -P)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask() { local p="$1" d="$2"; read -r -p "$p [$d]: " v; echo "${v:-$d}"; }
# No default, and no way past it. Coordinates are the one setting with no
# sensible fallback: a wrong sky looks exactly like a right one.
ask_required() {
  local p="$1" lo="$2" hi="$3" v=""
  while :; do
    if ! read -r -p "$p: " v; then
      # EOF (piped stdin, ssh without -t). Every other prompt falls back to
      # its default, but coordinates have no honest default — a wrong sky
      # looks exactly like a right one — so stop instead of looping forever.
      printf '\n  stdin closed before "%s" was answered. Run interactively,\n' "$p" >&2
      printf '  or create .env yourself from .env.example first.\n' >&2
      exit 1
    fi
    if printf '%s' "$v" | grep -qE '^-?[0-9]+(\.[0-9]+)?$' \
       && awk -v n="$v" -v lo="$lo" -v hi="$hi" 'BEGIN{exit !(n>=lo && n<=hi)}'; then
      echo "$v"; return
    fi
    printf '  needs a number between %s and %s\n' "$lo" "$hi" >&2
  done
}

ask_units() {
  local v=""
  while :; do
    v=$(ask "Units — f (Fahrenheit/mph) or c (Celsius/kmh)" "f")
    case "$v" in
      f|F) echo "f"; return;;
      c|C) echo "c"; return;;
    esac
    printf '  units must be f or c\n' >&2
  done
}

ask_yes_no() {
  local p="$1" d="$2" v=""
  while :; do
    v=$(ask "$p" "$d")
    case "$v" in
      y|Y|yes|Yes|YES) echo 1; return;;
      n|N|no|No|NO)    echo 0; return;;
    esac
    printf '  answer y or n\n' >&2
  done
}

validate_timezone() {
  "$UV_BIN" run --no-sync python - "$1" <<'PY'
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    if len(sys.argv[1]) > 255:
        raise ValueError
    ZoneInfo(sys.argv[1])
except (ZoneInfoNotFoundError, ValueError, OSError):
    raise SystemExit(1)
PY
}

ask_timezone() {
  local guess="$1" prompt="${2:-Timezone for those coordinates (IANA)}" v=""
  while :; do
    v=$(ask "$prompt" "$guess")
    if validate_timezone "$v"; then
      echo "$v"; return
    fi
    printf '  timezone must be an installed IANA name (for example, Europe/London)\n' >&2
  done
}

host_timezone_guess() {
  local candidate="" target=""

  # Respect an explicit process setting first, then the native Linux service,
  # Debian's conventional file, and the zoneinfo symlink used by Linux/macOS.
  # Abbreviations such as CST are rejected by ZoneInfo; only an IANA key earns
  # the label "detected host default" in the installer prompt.
  candidate="${TZ:-}"
  if [ -n "$candidate" ] && validate_timezone "$candidate"; then
    printf '%s\n' "$candidate"; return 0
  fi
  if command -v timedatectl >/dev/null; then
    candidate=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    if [ -n "$candidate" ] && validate_timezone "$candidate"; then
      printf '%s\n' "$candidate"; return 0
    fi
  fi
  if [ -r /etc/timezone ]; then
    candidate=$(sed -n '1p' /etc/timezone 2>/dev/null || true)
    if [ -n "$candidate" ] && validate_timezone "$candidate"; then
      printf '%s\n' "$candidate"; return 0
    fi
  fi
  target=$(readlink /etc/localtime 2>/dev/null || true)
  case "$target" in
    *zoneinfo/*) candidate="${target#*zoneinfo/}";;
    *) candidate="";;
  esac
  if [ -n "$candidate" ] && validate_timezone "$candidate"; then
    printf '%s\n' "$candidate"; return 0
  fi
  return 1
}

# `.env` is a data file consumed by the repo's parsers, never shell code. That
# distinction lets a URL query string contain '&' without becoming syntax.
# Keep the one-key-per-line invariant used by barkeep's config store.
validate_env_value() {
  local key="$1" value="$2"
  case "$value" in
    *$'\n'*|*$'\r'*)
      printf '  %s must be a single-line value\n' "$key" >&2
      return 1;;
  esac
}

write_env_line() {
  local key="$1" value="$2"
  printf '%s=%s\n' "$key" "$value"
}

run_root() {
  sudo "$@"
}

require_absolute_path() {
  local label="$1" path="$2"
  case "$path" in
    /*) ;;
    *)
      printf '  %s must be an absolute path: %s\n' "$label" "$path" >&2
      return 1;;
  esac
  validate_env_value "$label" "$path"
}

ensure_private_directory() {
  local path="$1"
  (
    umask 077
    mkdir -p -- "$path"
  )
  if [ -L "$path" ] || [ ! -d "$path" ] || [ ! -O "$path" ]; then
    printf '  runtime directory must be a real directory owned by %s: %s\n' \
      "$(id -un)" "$path" >&2
    return 1
  fi
  chmod 700 "$path"
}

verify_sha256() {
  "$UV_BIN" run --no-sync python - "$1" "$2" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
digest = hashlib.sha256()
with path.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
raise SystemExit(digest.hexdigest() != expected)
PY
}

verify_kokoro_install() {
  "$UV_BIN" run --no-sync python - "$1" <<'PY'
import sys

from busybar_dev.tts import verify_kokoro_synthesis

try:
    summary = verify_kokoro_synthesis(sys.argv[1])
except RuntimeError as exc:
    raise SystemExit(f"  {exc}") from exc
print(f"  {summary}")
PY
}

configured_kokoro_dir() {
  "$UV_BIN" run --no-sync python - <<'PY'
from busybar_dev.tts import configured_kokoro_dir

print(configured_kokoro_dir())
PY
}

linux_runtime_supported() {
  local arch="$1" libc="$2" major minor
  case "$arch" in
    x86_64|aarch64) ;;
    *) return 1;;
  esac
  if [[ ! "$libc" =~ ^glibc\ ([0-9]+)\.([0-9]+) ]]; then
    return 1
  fi
  major="${BASH_REMATCH[1]}"
  minor="${BASH_REMATCH[2]}"
  [ "$major" -gt 2 ] || {
    [ "$major" -eq 2 ] && [ "$minor" -ge 28 ]
  }
}

say "BUSY Bar Lab installer"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  say "Do not run this installer as root."
  echo "  Run ./deploy/install.sh as the unprivileged account that will own"
  echo "  the checkout and run barkeep. The installer invokes sudo only for"
  echo "  the package-manager and systemd operations that require it."
  exit 1
fi

HOST_OS=$(uname -s)
case "$HOST_OS" in
  Linux)
    HOST_ARCH=$(uname -m)
    HOST_LIBC=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
    if ! linux_runtime_supported "$HOST_ARCH" "$HOST_LIBC"; then
      say "Unsupported Linux production host."
      echo "  BUSY Bar Lab requires x86_64 or aarch64 Linux with glibc 2.28+."
      echo "  Detected architecture: $HOST_ARCH"
      echo "  Detected libc: ${HOST_LIBC:-unknown (getconf GNU_LIBC_VERSION unavailable)}"
      echo "  No packages, models, configuration, or services were changed."
      exit 1
    fi;;
  Darwin) ;;
  *)
    say "Unsupported operating system: $HOST_OS"
    echo "  BUSY Bar Lab supports Linux production and macOS direct development."
    echo "  No packages, models, configuration, or services were changed."
    exit 1;;
esac

# 1. Dependencies
#
# What must already be here. uv owns the locked Python environment, git is how
# deploy/ship.sh updates this checkout later, and every supported Linux install
# needs curl for the required hash-pinned model bank. Checked up front rather
# than failing halfway through.
missing=""
for tool in git; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
if [ "$HOST_OS" = "Linux" ]; then
  command -v curl >/dev/null || missing="$missing curl"
fi
UV_BIN=$(type -P uv || true)
if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
  if [ -n "${HOME:-}" ] && [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
  else
    missing="$missing uv"
  fi
fi
if [ -n "$missing" ]; then
  say "Missing required tools:$missing"
  echo "  Install them before running this script again."
  echo "  uv installation: https://docs.astral.sh/uv/getting-started/installation/"
  echo "  git is not optional even if you cloned by hand: deploy/ship.sh"
  echo "  updates the host by fetching into this checkout."
  exit 1
fi

# Decide whether this run owns a systemd install before changing the machine.
# A missing sudo binary or an unusable system manager must be discovered now,
# not after uv, models and private configuration have already been written.
SERVICE_USER=$(id -un)
SERVICE_UNIT="barkeep@$SERVICE_USER"
INSTALL_SVC=0
SERVICE_WAS_ACTIVE=0
if [ "$HOST_OS" = "Linux" ] && command -v systemctl >/dev/null; then
  if systemctl show --property=Version --value >/dev/null 2>&1; then
    if systemctl is-active --quiet "$SERVICE_UNIT" 2>/dev/null; then
      SERVICE_WAS_ACTIVE=1
      INSTALL_SVC=1
      say "barkeep is already installed — this run will refresh its unit."
    elif systemctl is-enabled --quiet "$SERVICE_UNIT" 2>/dev/null; then
      INSTALL_SVC=1
      say "barkeep is enabled but stopped — this run will refresh and start it."
    else
      INSTALL_SVC=$(ask_yes_no "Install barkeep so the apps run at boot? (y/n)" "y")
    fi
  else
    say "systemctl is installed but no usable system manager is running."
    echo "  Skipping service installation; run barkeep manually after setup."
  fi
fi

NEEDS_ROOT=0
[ "$INSTALL_SVC" -eq 1 ] && NEEDS_ROOT=1
if [ "$HOST_OS" = "Linux" ] && ! command -v espeak-ng >/dev/null; then
  for package_manager in apt-get dnf pacman apk zypper; do
    if command -v "$package_manager" >/dev/null; then
      NEEDS_ROOT=1
      break
    fi
  done
fi
ROOT_READY=0
if [ "$NEEDS_ROOT" -eq 1 ]; then
  if command -v sudo >/dev/null && sudo -v; then
    ROOT_READY=1
  elif [ "$INSTALL_SVC" -eq 1 ]; then
    say "Service installation needs working sudo access."
    echo "  Run as the intended unprivileged service account with working sudo,"
    echo "  then run the installer again. Do not run the installer as root."
    exit 1
  else
    say "No working sudo access — skipping the optional espeak-ng fallback."
  fi
fi

if [ "$INSTALL_SVC" -eq 1 ] && ! command -v systemd-analyze >/dev/null; then
  say "Service installation needs systemd-analyze."
  echo "  It validates the rendered unit before the live service is touched."
  exit 1
fi

# espeak-ng is emergency runtime resilience, not a substitute for the required
# production Kokoro check below. It is genuinely optional, so a host we cannot
# install it on gets a warning, not a dead
# installer. This used to run apt-get unconditionally on Linux, which aborted
# the whole script on any non-Debian distro, halfway, with uv already in.
if [ "$HOST_OS" = "Linux" ] && ! command -v espeak-ng >/dev/null; then
  say "Installing espeak-ng, the fallback speech engine (needs sudo)..."
  if [ "$ROOT_READY" -ne 1 ]; then
    :
  elif command -v apt-get >/dev/null; then
    run_root apt-get update -qq && run_root apt-get install -y -qq espeak-ng || true
  elif command -v dnf >/dev/null; then
    run_root dnf install -y -q espeak-ng || true
  elif command -v pacman >/dev/null; then
    run_root pacman -S --noconfirm --quiet espeak-ng || true
  elif command -v apk >/dev/null; then
    run_root apk add --quiet espeak-ng || true
  elif command -v zypper >/dev/null; then
    run_root zypper -q install -y espeak-ng || true
  fi
  command -v espeak-ng >/dev/null \
    || echo "  couldn't install espeak-ng — carrying on without it. Speech" \
            "still requires Kokoro to pass the production check below."
fi

# 2. Python environment. pyproject.toml constrains the complete supported
# stack to Python 3.11-3.13; on Linux the locked sync includes Kokoro.
say "Syncing the environment..."
"$UV_BIN" sync --locked

# Runtime storage is absent from a clean clone because it is intentionally
# gitignored. ReadWritePaths= requires every named path to exist before systemd
# enters the service namespace, so create and lock down all four classes now.
RUNTIME_CACHE_DIR="${BUSYBAR_CACHE_DIR:-$REPO_DIR/cache}"
RUNTIME_STATE_DIR="${BUSYBAR_STATE_DIR:-$REPO_DIR/state}"
require_absolute_path BUSYBAR_CACHE_DIR "$RUNTIME_CACHE_DIR"
require_absolute_path BUSYBAR_STATE_DIR "$RUNTIME_STATE_DIR"
for runtime_dir in \
  "$REPO_DIR/config" "$REPO_DIR/logs" \
  "$RUNTIME_CACHE_DIR" "$RUNTIME_STATE_DIR"
do
  ensure_private_directory "$runtime_dir"
done
UV_CACHE_DIR=$("$UV_BIN" cache dir)
require_absolute_path UV_CACHE_DIR "$UV_CACHE_DIR"
mkdir -p -- "$UV_CACHE_DIR"

# 3. Configuration. Semantic checks use the just-synced Python environment so
# the installer and Skystrip resolve timezone names through the same ZoneInfo.
if [ ! -f .env ]; then
  say "Let's configure this install (Enter accepts the default)."
  echo "  Your coordinates decide the whole sky — the sun's position, the"
  echo "  weather station, and local feed sampling. Find yours at https://latlong.net"
  LAT=$(ask_required "Latitude (decimal degrees, -90 to 90)" -90 90)
  LON=$(ask_required "Longitude (decimal degrees, -180 to 180)" -180 180)
  if TZG=$(host_timezone_guess); then
    TZV=$(ask_timezone "$TZG" \
      "Timezone for those coordinates (IANA; detected host default)")
  else
    echo "  Could not detect the host's IANA timezone. UTC is only a placeholder;"
    echo "  enter the zone for the place Skystrip will depict."
    TZV=$(ask_timezone "UTC" \
      "Timezone for those coordinates (IANA; UTC placeholder)")
  fi
  UNITS=$(ask_units)
  CONTACT=$(ask "Contact email/URL for the NWS User-Agent (blank ok)" "")
  HOSTV=$(ask "Bar address — blank for USB, or a LAN IP" "")
  TOKENV=""
  if [ -n "$HOSTV" ]; then TOKENV=$(ask "Bar access PIN (from its web UI)" ""); fi
  validate_env_value SKYSTRIP_TZ "$TZV"
  validate_env_value SKYSTRIP_UNITS "$UNITS"
  validate_env_value SKYSTRIP_CONTACT "$CONTACT"
  validate_env_value BUSYBAR_HOST "$HOSTV"
  validate_env_value BUSYBAR_TOKEN "$TOKENV"
  (
    # Secrets must be owner-only from the first byte, not merely after a later
    # chmod. Publish the complete parser file atomically from this same-dir
    # task-owned temporary path; interruption can leave only a 0600 temp file.
    umask 077
    BUSYBAR_ENV_TMP=$(mktemp ./.env.install.XXXXXX)
    trap 'rm -f -- "$BUSYBAR_ENV_TMP"' EXIT
    trap 'exit 1' HUP INT TERM
    {
      write_env_line SKYSTRIP_LAT "$LAT"
      write_env_line SKYSTRIP_LON "$LON"
      write_env_line SKYSTRIP_TZ "$TZV"
      # Live lightning is opt-in. The repo intentionally does not ship a raw
      # provider endpoint or presume that a fresh install has data rights.
      write_env_line SKYSTRIP_LIGHTNING_WS ""
      write_env_line SKYSTRIP_UNITS "$UNITS"
      write_env_line SKYSTRIP_CONTACT "$CONTACT"
      write_env_line SKYSTRIP_STATION ""
      write_env_line BUSYBAR_HOST "$HOSTV"
      write_env_line BUSYBAR_TOKEN "$TOKENV"
      # A new install is local-only. Operators who deliberately expose the UI
      # can later choose a LAN bind together with a strong BARKEEP_TOKEN.
      write_env_line BARKEEP_BIND "127.0.0.1"
      echo "# For LAN access set BARKEEP_BIND=0.0.0.0 plus BOTH of these:"
      echo "#   BARKEEP_TOKEN=   generate: uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'"
      echo "#   BARKEEP_TLS=1    HTTPS with a generated certificate; see SECURITY.md"
      echo "# Every other key is documented in .env.example."
    } > "$BUSYBAR_ENV_TMP"
    mv "$BUSYBAR_ENV_TMP" .env
    trap - EXIT HUP INT TERM
  )
  say "Wrote .env — edit it any time, then restart to apply."
else
  say ".env already exists — keeping it."
  echo "  (The configuration interview runs only when .env is absent. To be"
  echo "  asked again, move .env aside and rerun; or edit it by hand — every"
  echo "  key is documented in .env.example.)"
  # An install that predates the chmod above leaves the token world-readable.
  if [ -f .env ] && [ "$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null)" != "600" ]; then
    say "note: .env is readable by other local accounts. Fix with: chmod 600 .env"
  fi
fi
# BUSYBAR_TOKEN and the optional lightning endpoint can both be credentials.
# Keep existing installs private too, not only files created by this run.
chmod 600 .env

# 4. Kokoro's shared model and voice bank. It is required on the supported
# Linux production path; any download, digest, import, or synthesis failure is
# fatal and happens before a service is installed or started.
if [ "$HOST_OS" = "Linux" ]; then
  KOKORO_DIR=$(configured_kokoro_dir)
  require_absolute_path SKYSTRIP_VOICE_DIR "$KOKORO_DIR"
  if [ "$KOKORO_DIR" = "/" ]; then
    say "Unsafe SKYSTRIP_VOICE_DIR in .env: /"
    echo "  Choose a dedicated model directory and rerun the installer."
    exit 1
  fi
  if ! mkdir -p -- "$KOKORO_DIR"; then
    say "Could not create the configured Kokoro model directory."
    echo "  Check SKYSTRIP_VOICE_DIR in .env: $KOKORO_DIR"
    exit 1
  fi
  say "Using Kokoro model bank: $KOKORO_DIR"
  KOKORO_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
  CURL_RETRY_ARGS=(--retry 3)
  if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then
    CURL_RETRY_ARGS+=(--retry-all-errors)
  fi
  for item in \
    "kokoro-v1.0.onnx:7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5" \
    "voices-v1.0.bin:bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
  do
    f="${item%%:*}"
    expected="${item#*:}"
    dest="$KOKORO_DIR/$f"
    part="$KOKORO_DIR/.$f.part"
    if [ -f "$dest" ]; then
      if verify_sha256 "$dest" "$expected"; then
        continue
      fi
      # This path is installer-owned and its pinned digest did not match.
      # Remove it before retrying rather than leaving runtime to open
      # known-corrupt model bytes.
      rm -f "$dest"
    fi
    say "Downloading Kokoro model file $f..."
    rm -f "$part"
    if curl -L --fail "${CURL_RETRY_ARGS[@]}" -o "$part" \
        "$KOKORO_URL/$f" \
        && verify_sha256 "$part" "$expected"; then
      mv -f "$part" "$dest"
    else
      rm -f "$part"
      say "Kokoro download failed verification."
      echo "  Kokoro is required; system fallback speech cannot satisfy production."
      echo "  Check network access and free disk space, then rerun the installer."
      exit 1
    fi
  done

  say "Verifying required Kokoro speech..."
  if ! verify_kokoro_install "$KOKORO_DIR"; then
    say "Kokoro speech verification failed."
    echo "  Kokoro is required; system fallback speech cannot satisfy production."
    echo "  The Barkeep service was not installed or started by this run."
    exit 1
  fi
else
  say "Development speech engine check"
  "$UV_BIN" run --no-sync python - <<'PY'
from busybar_dev.tts import tts_engine_status

_engine, summary = tts_engine_status()
print(f"  {summary}")
PY
fi

# 5. Find the bar. connect() reads .env itself; do not source a data file.
say "Looking for your Busy Bar..."
if "$UV_BIN" run --no-sync python - <<'PY'
from busybar_dev import connect
try:
    with connect() as bb:
        bb.version()
    print("  found it.")
except Exception as e:
    raise SystemExit(f"  not reachable: {e}")
PY
then :; else
  say "Bar not reachable yet. Plug it in over USB or fix BUSYBAR_HOST in .env."
  echo "  Finishing host setup anyway; supervised apps will retry when it appears."
fi

# 6. Control-plane service (Linux + systemd only). The checked-in unit is a
# path-neutral template; render the actual checkout, uv, cache and state roots.
if [ "$INSTALL_SVC" -eq 1 ]; then
  SERVICE_TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/barkeep-unit.XXXXXX")
  SERVICE_TMP="$SERVICE_TMP_DIR/barkeep@.service"
  cleanup_service_tmp() {
    rm -f -- "$SERVICE_TMP"
    rmdir -- "$SERVICE_TMP_DIR" 2>/dev/null || :
  }
  trap cleanup_service_tmp EXIT HUP INT TERM
  "$UV_BIN" run --no-sync python deploy/render_service.py \
    --template deploy/barkeep.service \
    --checkout "$REPO_DIR" \
    --uv "$UV_BIN" \
    --cache-dir "$RUNTIME_CACHE_DIR" \
    --state-dir "$RUNTIME_STATE_DIR" \
    --uv-cache-dir "$UV_CACHE_DIR" \
    --output "$SERVICE_TMP"
  if ! systemd-analyze verify "$SERVICE_TMP"; then
    say "Rendered barkeep unit failed systemd validation."
    echo "  The installed unit and running service were left unchanged."
    exit 1
  fi
  # One parent owns every app process. A standalone skystrip unit would be a
  # second writer to the same bar, with doubled alerts/audio and asset races.
  run_root install -m 0644 "$SERVICE_TMP" \
    /etc/systemd/system/barkeep@.service
  run_root systemctl daemon-reload
  run_root systemctl enable "$SERVICE_UNIT"
  if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    run_root systemctl stop "$SERVICE_UNIT"
  fi
  run_root systemctl start "$SERVICE_UNIT"
  systemctl is-active --quiet "$SERVICE_UNIT"
  cleanup_service_tmp
  trap - EXIT HUP INT TERM
  say "Running. Watch it: journalctl -u $SERVICE_UNIT -f"
  say "Open the control plane"
  echo "  On this host: http://127.0.0.1:8080"
  echo "  From another computer, keep this SSH tunnel open:"
  echo "    ssh -N -L 8080:127.0.0.1:8080 $SERVICE_USER@server.example"
  echo "  Then open http://127.0.0.1:8080 locally and select Skystrip or DSN."
else
  say "Done."
  printf '  Run the control plane with: "%s" run -m barkeep\n' "$UV_BIN"
fi

say "The sky is yours."
