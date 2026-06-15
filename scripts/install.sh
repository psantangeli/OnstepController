#!/usr/bin/env bash
# Install the OnStep hand controller on a Raspberry Pi (Raspberry Pi OS Lite,
# Bookworm, 32-bit). Run from the repo root:  sudo ./scripts/install.sh
#
# Idempotent: safe to re-run after a `git pull`.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=onstep-handset
RUN_USER="${SUDO_USER:-pi}"

echo "==> Repo: $REPO_DIR   service user: $RUN_USER"

# 1. Enable the SPI bus the LCD needs (no-op if already on).
if command -v raspi-config >/dev/null 2>&1; then
  echo "==> Enabling SPI"
  raspi-config nonint do_spi 0
fi

# 2. System packages: Python venv tooling + libgpiod/lgpio backend + build bits
#    for any wheels that fall back to source.
echo "==> Installing apt dependencies"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3-venv python3-dev python3-pip \
  python3-lgpio \
  libfreetype6 libjpeg62-turbo libopenjp2-7 zlib1g \
  fonts-dejavu-core \
  avahi-daemon libnss-mdns          # lets onstep.local resolve for fast discovery
# ^ libfreetype6 is the runtime lib the piwheels Pillow font module needs;
#   without it Pillow's TrueType support fails to import.

# 3. Project venv (PEP 668: do not install into system Python on Bookworm).
#    --system-site-packages lets the venv see the apt-installed python3-lgpio.
echo "==> Creating virtualenv"
sudo -u "$RUN_USER" python3 -m venv --system-site-packages "$REPO_DIR/.venv"
sudo -u "$RUN_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade pip wheel
sudo -u "$RUN_USER" "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

# 4. Install + enable the systemd service.
echo "==> Installing systemd service"
install -m 644 "$REPO_DIR/systemd/${SERVICE}.service" "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable "$SERVICE"

echo
echo "Done. Edit config.yaml (set mount.host to your OnStep LAN IP), then:"
echo "  sudo systemctl restart $SERVICE"
echo "  journalctl -u $SERVICE -f"
