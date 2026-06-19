# OnStep WiFi Hand Controller

A handheld telescope controller for mounts running **OnStepX**. Runs on a
**Raspberry Pi Zero W** with a **Waveshare 1.3" LCD HAT** (240×240 ST7789
display, 5-way joystick, 3 keys) and talks to the mount over WiFi using OnStep's
LX200 command protocol.

The target mount uses a **FYSETC E4** board, which is ESP32-based and has **no
ST-4 port** — so motion is commanded over the network rather than a guide cable.

## What it does (v1)

- **Manual slew** — joystick drives the mount N/S/E/W (hold to move, release to
  stop). N/S = Dec, E/W = RA.
- **Selectable rate** — KEY1 / KEY3 cycle slew rate (Guide → Center → Find → Slew).
- **Emergency stop** — joystick centre press halts all motion (`:Q#`).
- **Settings menu** — press **KEY2** to open/close it. Joystick up/down selects a
  row, left/right changes its value, centre press or KEY2 closes it. Settings:
  - **Tracking** — rate: Off → Sidereal → Solar → Lunar (configurable; "king"
    available). Useful for solar observing.
  - **Brightness** — 3 levels, **remembered** across restarts.
  - **Park** — returns the mount to its power-on (home) position and stops
    tracking (sends `:hC#`). Requires a confirm (right to arm, right again to
    run). Assumes you always power on at the home position; see below.
  - **Update** — updates the *handset* software in place (`git pull` + deps),
    then the app restarts itself with the new code. Requires a confirm. Needs
    internet (works on your home WiFi). See below.
- **Night-vision friendly** — the screen is rendered **monochrome** (grey on
  black) so it stays readable behind a red filter. Since the HAT backlight is
  on/off only, "brightness" changes the grey intensity used to draw.
- **Live status screen** — RA/Dec, current rate, tracking / slewing / parked /
  **home** flags, and a connection/error banner.

GoTo, object catalogs, OnStep's saved-park, and focuser control are deferred.

### Park / end of session

The **Park** menu item is a simple "put it away" command: it sends OnStepX's
go-to-home (`:hC#`), which returns the mount to its **power-on position** and
turns tracking off automatically. The workflow it assumes:

1. **Always power on with the mount at the home position** (GEM counterweights
   down, pointing at the celestial pole). OnStepX (no homing sensors on the
   FYSETC E4) treats the power-on position as home.
2. Observe as normal — only ever move via the controller (don't release the
   clutches, or OnStepX loses track of where home is).
3. End of night: open the menu → **Park** → confirm. The mount slews home and
   stops tracking; the status screen shows **HOME**.

This deliberately does *not* use OnStep's saved-park (`:hP#`/`:hQ#`) feature —
it's just go-to-home, which is exactly "return to where it started".

### Update (from the handset)

The **Update** menu item updates the handset software without SSH: it runs
`git pull --ff-only` (and reinstalls Python deps only if `requirements.txt`
changed), then **exits so systemd relaunches it** with the new code — no sudo
needed (it never calls `systemctl` itself). Requirements:

- **Internet access** — works on your home WiFi; it can't pull in the field with
  no upstream.
- **A clean working tree** — keep your local changes in the gitignored
  `config.local.yaml` (overrides) so the tracked `config.yaml` stays pristine and
  the fast-forward pull never conflicts. Brightness, the discovered IP, etc. are
  already in gitignored files.

If the pull fails (no internet, or local edits to tracked files) it shows the
error on screen and keeps running — nothing is half-applied. This updates the
*handset* app, not the OnStep mount firmware.

### Controls

| Control | Normal mode | Settings menu |
|---|---|---|
| Joystick N/S/E/W | Slew (hold) | Up/Down select row, Left/Right change value |
| Joystick centre | Emergency STOP | Close menu |
| KEY1 / KEY3 | Slew rate down / up | Change selected value |
| KEY2 | Open settings menu | Close settings menu |

## Networking & auto-discovery

The mount (OnStep) and the Pi both join the **same WiFi** (your constant SSID).
Because the DHCP subnet changes between home and field, `mount.host` defaults to
**`auto`** and the controller *discovers* the mount on whatever network it joins:

1. **Cached IP** — the last known-good address (instant when unchanged).
2. **mDNS** — `onstep.local` / `onstepsws.local` (works when the mount is an ESP32
   with mDNS on — OnStepX default — and the Pi has avahi/libnss-mdns, installed
   by `install.sh`).
3. **Subnet sweep** — scans the local network for port 9999 and confirms each
   candidate with OnStep's `:GVP#` identity query. A /24 finishes in ~1–2 s.

The discovered IP is cached (`.discovered_host`) for a fast next boot, and the
controller automatically re-discovers if it can't reconnect (e.g. you moved
networks). To pin a fixed address instead, set `mount.host: "192.168.1.50"`.

It connects to `host:9999` (OnStep's LX200 command port) and auto-reconnects if
the link drops.

## Install (on the Pi)

```bash
git clone <this repo> ~/onstepController
cd ~/onstepController
# config.yaml defaults to mount.host: "auto" (discovery) -- usually no edit needed
sudo ./scripts/install.sh        # enables SPI, builds venv, mDNS, installs service
sudo systemctl restart onstep-handset
journalctl -u onstep-handset -f  # watch logs
```

## Run manually / dev

```bash
# On the Pi, in the foreground (Ctrl-C to quit):
.venv/bin/python -m onstep_handset.main -v

# Headless dry-run (no GPIO/LCD; logs status to console) — works off-Pi too:
.venv/bin/python -m onstep_handset.main --headless -v

# Protocol + comms tests (run anywhere, no hardware):
.venv/bin/python -m pytest tests/ -q
```

See `CLAUDE.md` for architecture and development details.
