#!/usr/bin/env bash
# ship.sh — deploy a commit to a host running barkeep.
#
# The host pulls from the configured git remote (origin by default). This
# script's only job on your machine is to prove the commit is actually there,
# then tell the host to go and get it.
#
# That ordering is the whole point. The remote is the source of truth, so a
# host can always rebuild itself from a clean clone without this laptop.
# An earlier version of this script pushed straight to a bare repo ON the
# target and the target's `origin` WAS that bare repo, which meant the only
# complete copy of the project lived on whichever machine you happened to be
# sitting at.
#
#   ./deploy/ship.sh                      # HEAD -> $BUSYBAR_DEPLOY_HOST
#   ./deploy/ship.sh --ref v1.2 pi.local  # a tag, to a named host
#   ./deploy/ship.sh --dry-run            # print what would happen, touch nothing
#
# Configuration, all optional except the host:
#   BUSYBAR_DEPLOY_HOST     ssh target (or pass it as the last argument)
#   BUSYBAR_DEPLOY_PATH     checkout on the host      (default: busybar-lab)
#   BUSYBAR_DEPLOY_SERVICE  systemd unit              (default: barkeep@$USER)
#   BUSYBAR_DEPLOY_REMOTE   git remote                (default: origin)
#   BUSYBAR_DEPLOY_BRANCH   branch it must be on      (default: main)
#
# Only committed work ships. Config never travels: .env and config/ are
# gitignored and belong to the host.
set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE="${BUSYBAR_DEPLOY_REMOTE:-origin}"
BRANCH="${BUSYBAR_DEPLOY_BRANCH:-main}"
DIR="${BUSYBAR_DEPLOY_PATH:-busybar-lab}"
# A trailing '@' means "instance name is the user on the HOST", resolved
# there rather than here. Interpolating $USER locally would deploy under
# whoever is sitting at this laptop; quoting it for the remote shell to
# expand is worse, because a single-quoted $USER never expands at all and
# systemd is handed a unit literally called barkeep@$USER. Ask the host.
SERVICE="${BUSYBAR_DEPLOY_SERVICE:-barkeep@}"
REF="HEAD"
DRY_RUN=0
HOST="${BUSYBAR_DEPLOY_HOST:-}"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
note() { printf '  %b\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --ref)     REF="${2:?--ref needs a commit, tag or branch}"; shift 2 ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail$/{ /^set -euo pipefail$/q; p; }' "$0" \
        | sed 's/^# \{0,1\}//'
      exit 0;;
    -*)        die "unknown option: $1" ;;
    *)         HOST="$1"; shift ;;
  esac
done

[ -n "$HOST" ] || die "no host. Pass one as an argument or set BUSYBAR_DEPLOY_HOST.
       e.g. ./deploy/ship.sh pi.local"

SHA=$(git rev-parse --verify --quiet "$REF^{commit}") \
  || die "no such commit: $REF"
SHORT=$(git rev-parse --short "$SHA")

if ! git diff --quiet || ! git diff --cached --quiet; then
  note "note: uncommitted changes stay behind — shipping $SHORT as committed."
fi

git remote get-url "$REMOTE" >/dev/null 2>&1 \
  || die "no '$REMOTE' remote. Add one:
       git remote add $REMOTE <url>"

if [ "$DRY_RUN" -eq 0 ]; then
  git fetch --quiet "$REMOTE" "$BRANCH" \
    || die "cannot reach $REMOTE. Check the network and deploy credentials."
fi

# THE guard. If the commit is not on origin, the host cannot fetch it, and
# deploying it any other way would put the host somewhere unreproducible.
if ! git merge-base --is-ancestor "$SHA" "$REMOTE/$BRANCH" 2>/dev/null; then
  die "$SHORT is not on $REMOTE/$BRANCH, so the host cannot fetch it.

       Push it first:   git push $REMOTE HEAD:$BRANCH"
fi

# `git fetch` then `reset --hard` rather than `git pull`: the host is a deploy
# target, not a working copy. Anything edited there is discarded on purpose,
# and a merge conflict on a machine nobody is sitting at helps no one. Sync
# the exact locked environment before restart so new code never starts against
# dependencies from the previous release.
REMOTE_CMD="set -e
cd '$DIR'
unit='$SERVICE'
case \"\$unit\" in *@) unit=\"\$unit\$(id -un)\";; esac
git fetch --quiet '$REMOTE'
if command -v uv >/dev/null 2>&1; then
  uv_bin=\$(command -v uv)
elif [ -x \"\$HOME/.local/bin/uv\" ]; then
  uv_bin=\"\$HOME/.local/bin/uv\"
else
  echo \"error: uv not found on PATH or at \$HOME/.local/bin/uv\" >&2
  exit 1
fi
# Applying a unit is root-equivalent and deliberately remains outside this
# deploy account's narrow stop/start permission. install.sh renders host paths and
# refreshes it. Check the TARGET commit's template before changing the live
# checkout or environment: an old daemon may still spawn children while this
# command runs, and those children must not see half of the next release.
installed=\$(systemctl show --property=FragmentPath --value \"\$unit\" \\
  2>/dev/null || true)
expected_unit_hash=\$(
  {
    printf 'template\0'
    git show '$SHA:deploy/barkeep.service'
    printf '\0renderer\0'
    git show '$SHA:deploy/render_service.py'
  } | \"\$uv_bin\" run --no-sync python -c \
    'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
)
installed_unit_hash=
if [ -n \"\$installed\" ] && [ -r \"\$installed\" ]; then
  IFS= read -r installed_unit_header < \"\$installed\" || true
  case \"\$installed_unit_header\" in
    '# busybar-unit-contract-sha256='*) installed_unit_hash=\"\${installed_unit_header#*=}\";;
  esac
fi
if [ -z \"\$installed_unit_hash\" ] \\
   || [ \"\$expected_unit_hash\" != \"\$installed_unit_hash\" ]; then
  echo \"\" >&2
  echo \"  ============================================================\" >&2
  echo \"  ERROR: the installed service unit is missing or out of date.\" >&2
  echo \"  The live checkout and environment were left unchanged.\" >&2
  echo \"  Apply this unit-changing release interactively on the host:\" >&2
  echo \"    sudo systemctl stop \\\"\$unit\\\"  # if it is currently running\" >&2
  echo \"    git reset --hard $SHA\" >&2
  echo \"    ./deploy/install.sh\" >&2
  echo \"  ============================================================\" >&2
  exit 1
fi
sudo systemctl stop \"\$unit\"
git reset --hard --quiet $SHA
\"\$uv_bin\" sync --locked
if ! \"\$uv_bin\" run --no-sync python -c \
  'from busybar_dev.tts import configured_kokoro_dir, verify_kokoro_synthesis; print(\"  \" + verify_kokoro_synthesis(configured_kokoro_dir()))'
then
  echo \"error: required Kokoro speech verification failed\" >&2
  echo \"  The service remains stopped. Rerun ./deploy/install.sh on this host.\" >&2
  exit 1
fi
sudo systemctl start \"\$unit\"
sleep 3
systemctl is-active --quiet \"\$unit\"
echo \"  \$(git rev-parse --short HEAD) live on \$(hostname) as \$unit\""

if [ "$DRY_RUN" -eq 1 ]; then
  echo "would deploy $SHORT to $HOST:$DIR"
  echo "would run on $HOST:"
  printf '%s\n' "$REMOTE_CMD" | sed 's/^/    /'
  exit 0
fi

# shellcheck disable=SC2029  # expanding SHA/DIR locally is the intent
ssh "$HOST" "$REMOTE_CMD"

# The instance name must resolve on the HOST (same reasoning as SERVICE
# above), so the hint quotes the whole command for the remote shell; a bare
# \$(id -un) here would expand on whatever laptop the operator pastes it into.
case "$SERVICE" in
  *@) echo "shipped $SHORT. watch: ssh $HOST 'journalctl -u \"${SERVICE}\$(id -un)\" -f'" ;;
  *)  echo "shipped $SHORT. watch: ssh $HOST 'journalctl -u \"$SERVICE\" -f'" ;;
esac
