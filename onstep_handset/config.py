"""Load configuration from config.yaml (with optional config.local.yaml override)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(_ROOT, "config.yaml")
LOCAL_CONFIG = os.path.join(_ROOT, "config.local.yaml")
#: Where the last successfully-discovered mount IP is remembered.
HOST_CACHE = os.path.join(_ROOT, ".discovered_host")
#: Persisted UI settings (e.g. brightness level), JSON.
SETTINGS_PATH = os.path.join(_ROOT, ".ui_settings.json")


@dataclass
class Config:
    host: str
    port: int
    connect_timeout: float
    backoff_min: float
    backoff_max: float
    poll_hz: float
    ui_fps: float
    spi_hz: int
    rotation: int
    slew_rates: list[str]
    default_rate_index: int
    pins: dict[str, int] = field(default_factory=dict)
    tracking_modes: list[str] = field(
        default_factory=lambda: ["off", "sidereal", "solar", "lunar"])
    default_tracking_index: int = 0
    # Discovery (used when host == "auto").
    discovery_hostnames: list[str] = field(default_factory=lambda: ["onstep.local"])
    discovery_subnet_prefix: int = 24
    discovery_scan_timeout: float = 0.3
    discovery_cache: bool = True
    # Monochrome "brightness" = grey-intensity multipliers (HAT backlight is
    # on/off only). Cycled in the settings menu; the chosen index is persisted.
    brightness_levels: list[float] = field(default_factory=lambda: [0.35, 0.65, 1.0])
    default_brightness_index: int = 1


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> Config:
    """Load config from ``path`` (default config.yaml), merging config.local.yaml."""
    path = path or DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if path == DEFAULT_CONFIG and os.path.exists(LOCAL_CONFIG):
        with open(LOCAL_CONFIG, "r", encoding="utf-8") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})

    mount = data.get("mount", {})
    poll = data.get("poll", {})
    ui = data.get("ui", {})
    disc = mount.get("discovery", {})

    cfg = Config(
        host=str(mount["host"]),
        port=int(mount.get("port", 9999)),
        connect_timeout=float(mount.get("connect_timeout", 4.0)),
        backoff_min=float(mount.get("backoff_min", 0.5)),
        backoff_max=float(mount.get("backoff_max", 8.0)),
        poll_hz=float(poll.get("hz", 2.0)),
        ui_fps=float(ui.get("fps", 5.0)),
        spi_hz=int(ui.get("spi_hz", 32_000_000)),
        rotation=int(ui.get("rotation", 0)),
        slew_rates=list(data.get("slew_rates", ["RG", "RC", "RM", "RS"])),
        default_rate_index=int(data.get("default_rate_index", 0)),
        pins=dict(data.get("pins", {})),
        tracking_modes=list(data.get("tracking_modes",
                                     ["off", "sidereal", "solar", "lunar"])),
        default_tracking_index=int(data.get("default_tracking_index", 0)),
        discovery_hostnames=list(disc.get("hostnames", ["onstep.local", "onstepsws.local"])),
        discovery_subnet_prefix=int(disc.get("subnet_prefix", 24)),
        discovery_scan_timeout=float(disc.get("scan_timeout", 0.3)),
        discovery_cache=bool(disc.get("cache", True)),
        brightness_levels=[float(x) for x in ui.get("brightness_levels", [0.35, 0.65, 1.0])],
        default_brightness_index=int(ui.get("default_brightness_index", 1)),
    )
    if not cfg.slew_rates:
        raise ValueError("slew_rates must not be empty")
    if not cfg.tracking_modes:
        raise ValueError("tracking_modes must not be empty")
    if not cfg.brightness_levels:
        raise ValueError("brightness_levels must not be empty")
    cfg.default_rate_index = max(0, min(cfg.default_rate_index, len(cfg.slew_rates) - 1))
    cfg.default_tracking_index = max(
        0, min(cfg.default_tracking_index, len(cfg.tracking_modes) - 1))
    cfg.default_brightness_index = max(
        0, min(cfg.default_brightness_index, len(cfg.brightness_levels) - 1))
    return cfg
