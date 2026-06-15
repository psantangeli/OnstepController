#!/usr/bin/env bash
# Update the hand controller on the Pi: pull latest code, refresh deps if the
# requirements changed, and restart the service. Run from anywhere:
#
#   ~/onstepController/scripts/update.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=onstep-handset
cd "$REPO_DIR"

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
