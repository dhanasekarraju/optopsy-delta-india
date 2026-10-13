"""Single operator setting; LIVE request fails closed until an audited release."""
from __future__ import annotations

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
