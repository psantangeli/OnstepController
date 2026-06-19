# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A WiFi hand controller for telescope mounts running **OnStepX**. It runs on a
**Raspberry Pi Zero W v1.1** (single-core ARMv6, 512 MB) with a **Waveshare 1.3"
LCD HAT** (240×240 ST7789 display, 5-way joystick, 3 keys) and commands the mount
over TCP using OnStep's LX200 protocol. The target mount uses a **FYSETC E4**
(ESP32) board, which has **no ST-4 port** — all motion is sent over WiFi.

v1 scope: manual slewing, rate selection, emergency stop, tracking toggle, and a
live status screen. GoTo/catalogs/park/focuser are intentionally deferred.

## Commands

```bash
# Tests — pure protocol + mock-server comms tests; run anywhere, no hardware:
.venv/bin/python -m pytest tests/ -q

# Headless dry-run — no GPIO/LCD, logs mount status to console. Works off-Pi
# (point config.yaml mount.host at a reachable OnStep, or it logs disconnected):
.venv/bin/python -m onstep_handset.main --headless -v

# Run on the Pi (foreground):
.venv/bin/python -m onstep_handset.main -v

# As a service on the Pi:
sudo ./scripts/install.sh                 # SPI + venv + systemd (idempotent)
sudo systemctl restart onstep-handset
journalctl -u onstep-handset -f
```

A `.venv` for off-Pi development needs only `pytest` and `pyyaml`
(`python3 -m venv .venv && .venv/bin/pip install pytest pyyaml`). The hardware
libs (`luma.lcd`, `gpiozero`, `lgpio`) are imported lazily, so tests and the
`--headless` path run without them.

## Architecture

Threading model — two long-lived threads, joined by a thread-safe queue and a
lock-guarded state object:

```
GPIO callbacks ──Action──▶ queue.Queue ──▶ [comms thread] ──▶ SharedState ──▶ [UI loop]
 (inputs.py)                                (main.CommsWorker)   (state.py)     (display.py)
                                                  │
                                                  └─ owns the ONLY socket (comms.py)
```

- **`main.CommsWorker`** (comms thread) is the sole owner of the TCP socket. It
  resolves the mount address (via `HostResolver`), drains the action queue
  (move/stop/rate/track writes) and polls `:GU#`/`:GR#`/`:GD#` on an interval,
  writing results into `SharedState`. It also owns reconnect-with-backoff and
  re-discovery after `REDISCOVER_AFTER` consecutive connect failures. Keeping all
  socket writes here means GPIO callbacks never touch the network.
- **`_ui_loop`** (main thread) reads `SharedState` snapshots and repaints the LCD
  **only on change** (`Display.render` diffs a render key).
- **`inputs.py`** GPIO callbacks just enqueue `Action`s — no I/O.

Why threads work on a single ARMv6 core: the work is I/O-bound and socket reads
release the GIL, so the UI isn't starved during a poll. Keep poll ~1–3 Hz and UI
≤5 Hz (configured in `config.yaml`).

## Files

- `onstep_handset/protocol.py` — **pure** LX200 command builders + parsers. No
  I/O, no hardware → all logic is unit-tested here. The `:GU#` status string is
  **position-independent**: scan for character *presence*, never fixed offsets.
- `onstep_handset/comms.py` — `OnStepClient`: persistent TCP socket, `#`-framed
  reads, TCP keepalive, exponential-backoff reconnect. Raises `ConnectionError`
  on any disconnect; the worker loop catches it and reconnects. `host` is mutable
  and may be `None` (set by the worker after resolution).
- `onstep_handset/discovery.py` — finds the mount when `mount.host == "auto"`.
  Cascade: cached IP → mDNS hostname (`onstep.local`) → subnet sweep, confirming
  every candidate with `:GVP#` (`On-Step#`). `HostResolver` wraps config (a fixed
  IP is returned as-is). Discovered IP is cached to `.discovered_host`.
- `onstep_handset/state.py` — frozen `MountState` snapshot + `SharedState`
  (single `Lock`, comms thread writes, UI reads).
- `onstep_handset/inputs.py` — gpiozero `Button`s (active-low, `pull_up=True`) →
  `Action`s on the queue. Joystick = hold-to-move/release-to-stop. **KEY2** emits
  `MENU` (toggle settings menu); KEY1/KEY3 are slew rate down/up.
- `onstep_handset/display.py` — luma.lcd ST7789 wrapper + `render(state)`.
  **Monochrome** (grey on black, for a red night-vision filter); all greys are
  scaled by the current brightness factor. Renders the status screen or, when
  `menu_open`, the settings menu. `MENU_ITEMS` lives here (imported by main.py).
- `onstep_handset/settings.py` — tiny JSON persistence for user-set values
  (brightness), to `.ui_settings.json`. Separate from `config.yaml` (committed
  defaults) because these change on the device and must stick across restarts.
- `onstep_handset/main.py` — config load, thread wiring, signal handling, the
  `CommsWorker` and UI loop. The worker owns menu state: `MENU` (KEY2) toggles
  the menu (and sends `:Q#`); while `menu_open`, controls navigate the menu
  (rows: Tracking, Brightness) instead of the mount; brightness is persisted via
  `settings.py`, tracking-rate changes are sent to the mount.
- `onstep_handset/config.py` / `config.yaml` — config + optional
  `config.local.yaml` override (gitignored). `brightness_levels` are grey-intensity
  multipliers (the HAT backlight is on/off only, so brightness == grey level).

## Hardware facts (don't re-derive these)

- **Display is ST7789 at true 240×240** → `h_offset=0, v_offset=0`. The
  `y_offset=80` quirk applies only to the 240×135 variants; do **not** add it here.
- **Waveshare HAT BCM pin map** (all inputs active-low + internal pull-up), in
  `config.yaml` under `pins:` — display DC=25, RST=27, BL=24; joystick
  UP=6 DOWN=19 LEFT=5 RIGHT=26 PRESS=13; KEY1=21 KEY2=20 KEY3=16. SPI0 / CE0.
- **OnStep axes:** n/s = Dec, e/w = RA. LX200 motion commands send no reply;
  `:GU#`/`:GR#`/`:GD#` are `#`-terminated. Rate codes slow→fast: RG/RC/RM/RS.
- **Discovery identity:** `:GVP#` returns `On-Step#` for *both* OnStep and
  OnStepX (not "OnStepX"); `:GVN#` starts `10.` for OnStepX. mDNS is ESP32-only
  and advertises a **hostname** (`onstep.local`), not a browsable service — so we
  resolve the name / sweep + `:GVP#`, never a zeroconf service browse.

## Platform constraints

- Pi Zero W is **ARMv6 → 32-bit Raspberry Pi OS only** (arm64 won't boot).
- **Bookworm**: install Python deps in a **venv** (PEP 668 blocks system pip);
  use the **lgpio** gpiozero backend (the install script pulls `python3-lgpio`
  and builds the venv with `--system-site-packages` so it's visible).
- Full-frame LCD pushes are slow on ARMv6 — rely on redraw-on-change, never a
  per-frame full repaint.
