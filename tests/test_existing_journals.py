"""No side-effect journal inspection and explicit product binding."""
import sqlite3

from optopsy_delta.existing_journals import (
    existing_exit_status,
    existing_lifecycle_status,
)
from optopsy_delta.exit_recovery import ExitAttemptJournal
from optopsy_delta.position_lifecycle import LifecycleJournal


def test_missing_path_never_creates_database(tmp_path):
    first = tmp_path / "missing-life.sqlite"
    second = tmp_path / "missing-exit.sqlite"
    assert existing_lifecycle_status(first) is None
    assert existing_exit_status(second) is None
    assert not first.exists() and not second.exists()


def test_existing_journals_read_without_mutation(tmp_path):
    life = LifecycleJournal(tmp_path / "life.sqlite")
    life.register_flat_baseline(
        product_id=44, baseline_us=100,
        account_positions=[], active_orders=[],
    )
    exits = ExitAttemptJournal(tmp_path / "exit.sqlite")
    before_life = life.path.read_bytes()
    before_exit = exits.path.read_bytes()
    status = existing_lifecycle_status(life.path)
    assert status["state"] == "OBSERVING"
    assert status["product_id"] == 44
    assert existing_exit_status(exits.path)["attempts"] == []
    assert life.path.read_bytes() == before_life
    assert exits.path.read_bytes() == before_exit


def test_invalid_or_symlinked_files_are_not_trusted(tmp_path):
    raw = tmp_path / "corrupt.sqlite"
    raw.write_text("not sqlite")
    assert existing_lifecycle_status(raw)["state"] == "UNAVAILABLE"
    assert existing_exit_status(raw)["attempts"][0]["state"] == "INVALID"
    link = tmp_path / "alias.sqlite"
    link.symlink_to(raw)
    assert existing_lifecycle_status(link) is None
    assert existing_exit_status(link) is None


def test_existing_exit_pending_detectable(tmp_path):
    j = ExitAttemptJournal(tmp_path / "exit.sqlite")

    candidate = {
        "status": "DRAFT_EXIT_ONLY", "submit_authorized": False,
        "side": "sell", "reduce_only": True, "order_type": "limit_order",
        "time_in_force": "ioc", "product_id": 44,
        "remaining_contracts": 2, "draft_size": 2,
        "available_bid_depth": 2, "quote_observed_unix": 100,
        "limit_price": "20", "reason": "PRE_EXPIRY_SAFETY_CUTOFF",
    }
    j.prepare(candidate)
    assert existing_exit_status(j.path)["attempts"][-1]["state"] == "PREPARED_UNKNOWN"
    with sqlite3.connect(j.path) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_safety_status_cli_accepts_explicit_product_and_existing_paths(monkeypatch, capsys):
    import json

    from optopsy_delta import cli

    monkeypatch.setattr(
        "optopsy_delta.safety_watchdog.read_only_watchdog_check",
        lambda **kwargs: {
            "product": kwargs["product_id"],
            "lifecycle": kwargs["lifecycle_status"],
            "exits": kwargs["exit_status"],
        },
    )
    assert cli.main(["safety-status", "--product-id", "44"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["product"] == 44
    assert response["lifecycle"] is None
    assert response["exits"] is None
