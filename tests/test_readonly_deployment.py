"""Smoke checks for read-only public data capture."""
from pathlib import Path
from types import SimpleNamespace

from optopsy_delta import cli


def test_capture_zero_usable_quotes_returns_failure_and_preserves_snapshot(monkeypatch, capsys):
    writes = []
    monkeypatch.setattr(
        cli, "DeltaClient",
        lambda: SimpleNamespace(
            live_products=lambda: [{"id": 1}],
            option_tickers=lambda underlying: [{"product_id": 1}],
        ),
    )
    monkeypatch.setattr(cli, "snapshot_to_rows", lambda *args, **kwargs: [])

    def save(*args, **kwargs):
        writes.append(kwargs)
        return Path("raw.json")

    monkeypatch.setattr(cli, "write_snapshot", save)
    assert cli.main(["capture", "--underlying", "BTC"]) == 3
    assert len(writes) == 1
    assert writes[0]["underlying"] == "BTC"
    assert "zero usable quotes" in capsys.readouterr().out


def test_capture_usable_quotes_success(monkeypatch):
    monkeypatch.setattr(
        cli, "DeltaClient",
        lambda: SimpleNamespace(
            live_products=lambda: [{"id": 1}],
            option_tickers=lambda underlying: [{"product_id": 1}],
        ),
    )
    monkeypatch.setattr(
        cli, "snapshot_to_rows", lambda *args, **kwargs: [{"bid": 1, "ask": 2}]
    )
    monkeypatch.setattr(cli, "write_snapshot", lambda *args, **kwargs: Path("raw.json"))
    assert cli.main(["capture", "--underlying", "ETH"]) == 0
