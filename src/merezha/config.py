"""Configuration: targets, intervals and detector settings.

Configuration lives in a small TOML file (see :data:`CONFIG_TEMPLATE`).
If no file is found, a sensible default set of public targets is used so
``merezha monitor`` works out of the box.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".merezha"
DEFAULT_CONFIG_PATH = Path("merezha.toml")


@dataclass(frozen=True)
class Target:
    """A single endpoint to watch."""

    name: str
    host: str
    port: int = 443
    kind: str = "http"  # "http" (full waterfall) or "tcp" (connect-only)


@dataclass
class Config:
    interval: float = 10.0
    history_window: int = 40
    z_threshold: float = 3.5
    min_train_samples: int = 120
    data_dir: Path = DEFAULT_DATA_DIR
    targets: list[Target] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "merezha.db"


DEFAULT_TARGETS = [
    Target("google", "google.com", 443, "http"),
    Target("cloudflare-dns", "1.1.1.1", 53, "tcp"),
    Target("github", "github.com", 443, "http"),
]

CONFIG_TEMPLATE = """\
# merezha.toml — Merezha configuration
# Docs: https://github.com/illiatkachuk/merezha

# Seconds between probe rounds.
interval = 10.0

# Robust z-score threshold for the statistical detector.
# 3.5 is a common conservative default; lower it for a more sensitive setup.
z_threshold = 3.5

# Samples per target required before the IsolationForest model activates.
min_train_samples = 120

# Where the SQLite database is stored (defaults to ~/.merezha).
# data_dir = "~/.merezha"

[[targets]]
name = "google"
host = "google.com"
port = 443
kind = "http"   # full waterfall: DNS -> TCP -> TLS -> TTFB

[[targets]]
name = "cloudflare-dns"
host = "1.1.1.1"
port = 53
kind = "tcp"    # connect-only latency (works for any TCP service)

[[targets]]
name = "github"
host = "github.com"
port = 443
kind = "http"
"""


def default_db_path() -> Path:
    return DEFAULT_DATA_DIR / "merezha.db"


def load_config(path: Path | None = None) -> Config:
    """Load configuration from *path*, falling back to built-in defaults.

    A missing file is not an error: Merezha should be usable with zero
    setup, so the default public targets are returned instead.
    """
    cfg = Config(targets=list(DEFAULT_TARGETS))
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return cfg

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg.interval = float(raw.get("interval", cfg.interval))
    cfg.history_window = int(raw.get("history_window", cfg.history_window))
    cfg.z_threshold = float(raw.get("z_threshold", cfg.z_threshold))
    cfg.min_train_samples = int(raw.get("min_train_samples", cfg.min_train_samples))
    if "data_dir" in raw:
        cfg.data_dir = Path(str(raw["data_dir"])).expanduser()

    targets = []
    for item in raw.get("targets", []):
        targets.append(
            Target(
                name=str(item["name"]),
                host=str(item["host"]),
                port=int(item.get("port", 443)),
                kind=str(item.get("kind", "http")).lower(),
            )
        )
    if targets:
        cfg.targets = targets
    return cfg


def write_template(path: Path, force: bool = False) -> bool:
    """Write the config template to *path*. Returns ``False`` if it already exists."""
    if path.exists() and not force:
        return False
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True
