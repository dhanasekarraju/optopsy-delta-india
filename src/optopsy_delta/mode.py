"""Single operator setting. LIVE needs TWO operator-set interlocks (config + environment)."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path


class ModeError(RuntimeError):
    pass


def paper_enabled(path: str | Path = "config.toml") -> bool:
    source = Path(path)
    if source.is_symlink():
        raise ModeError("Symlinked mode configuration is not accepted")
    try:
        with source.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModeError("Configuration missing or invalid; cannot start bot") from exc
    if set(config) != {"paper_enabled"} or type(config["paper_enabled"]) is not bool:
        raise ModeError("Expected exactly one boolean: paper_enabled")
    return config["paper_enabled"]


def require_paper(path: str | Path = "config.toml") -> None:
    if not paper_enabled(path):
        # Explicitly never infer that false authorizes a financial transaction.
        raise ModeError("LIVE_REQUESTED_BUT_EXECUTION_NOT_RELEASED; no real orders are enabled")


LIVE_ENV = "OPTOPSY_LIVE_ENABLED"
LIVE_VALUE = "YES_REAL_ORDERS"


def require_live(path: str | Path = "config.toml") -> None:
    """Live worker gate: BOTH operator-controlled interlocks must be set explicitly.

    1. config.toml: paper_enabled = false   (paper worker refuses to run in this state)
    2. environment: OPTOPSY_LIVE_ENABLED=YES_REAL_ORDERS
    Anything else fails closed. Nothing in this code sets either interlock.
    """
    if paper_enabled(path):
        raise ModeError("PAPER_MODE_ACTIVE; set paper_enabled = false to request live mode")
    if os.getenv(LIVE_ENV, "") != LIVE_VALUE:
        raise ModeError("LIVE_ENV_INTERLOCK_NOT_SET; real orders remain disabled")
