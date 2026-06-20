#!/bin/sh
# Update the hand controller on the Pi: pull latest code, refresh deps if the
# requirements changed, and restart the service. Run from anywhere:
#
#   ~/onstepController/scripts/update.sh        (or: sh ~/onstepController/scripts/update.sh)
#
# POSIX sh -- works under both bash and dash (Pi OS /bin/sh).
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=onstep-handset
cd "$REPO_DIR"

# This directory must be a git clone (the in-app Update needs this too). If it
# was set up by copying files, convert it in place -- see the message below.
if ! git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: $REPO_DIR is not a git clone, so it can't pull updates." >&2
  echo "Convert it in place (keeps your .venv and local files):" >&2
  echo "  cd \"$REPO_DIR\"" >&2
  echo "  git init -b main" >&2
  echo "  git remote add origin https://github.com/psantangeli/OnstepController.git" >&2
  echo "  git fetch origin" >&2
  echo "  git reset --hard origin/main      # overwrites tracked files w/ repo versions" >&2
  echo "  git branch --set-upstream-to=origin/main main" >&2
  echo "(Local overrides in config.local.yaml / .venv / .ui_settings.json are gitignored and kept.)" >&2
  exit 1
fi

# Remember the requirements hash so we only rebuild the venv when it changes.
req_before="$(git hash-object requirements.txt 2>/dev/null || true)"

echo "==> Pulling latest from $(git config --get remote.origin.url)"
git pull --ff-only

req_after="$(git hash-object requirements.txt 2>/dev/null || true)"
if [ "$req_before" != "$req_after" ] && [ -x "$REPO_DIR/.venv/bin/pip" ]; then
  echo "==> requirements.txt changed; updating dependencies"
  "$REPO_DIR/.venv/bin/pip" install -r requirements.txt
fi

# Restart the service if it's installed; otherwise just report.
if systemctl list-unit-files | grep -q "^${SERVICE}.service"; then
  echo "==> Restarting $SERVICE"
  sudo systemctl restart "$SERVICE"
  sleep 1
  systemctl --no-pager --lines=0 status "$SERVICE" | head -3
  echo "    (live logs: journalctl -u $SERVICE -f)"
else
  echo "==> $SERVICE not installed as a service; run scripts/install.sh first."
fi

echo "Done."
