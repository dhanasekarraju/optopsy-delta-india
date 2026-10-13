"""Single mode setting and read-only paper dashboard reporting."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from optopsy_delta.mode import ModeError, paper_enabled, require_paper
from optopsy_delta.paper_reporting import read_paper_status
from optopsy_delta.paper_worker import PaperLedger

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def test_single_switch_defaults_paper_and_false_is_locked(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("paper_enabled = true\n")
    assert paper_enabled(path) is True
    require_paper(path)
    path.write_text("paper_enabled = false\n")
    assert paper_enabled(path) is False
    with pytest.raises(ModeError, match="LIVE_REQUESTED"):
        require_paper(path)


def test_mode_invalid_or_missing_fails_closed(tmp_path):
    path = tmp_path / "config.toml"
    with pytest.raises(ModeError):
        require_paper(path)
    for value in ("paper_enabled = 'true'\n", "paper_enabled = 1\n",
                  "paper_enabled = true\nextra = 1\n"):
        path.write_text(value)
        with pytest.raises(ModeError):
            require_paper(path)


def test_reporting_missing_db_does_not_create_it(tmp_path):
    path = tmp_path / "missing.sqlite"
    assert read_paper_status(path)["status"] == "NO_LEDGER"
    assert not path.exists()


def test_reporting_counts_round_trip_and_fees(tmp_path):
    path = tmp_path / "journal.sqlite"
    ledger = PaperLedger(path)
    contract = {"underlying": "BTC", "product_id": 44,
                "contract_value": "0.01",
                "expiry_utc": (NOW + timedelta(days=7)).isoformat()}
    ledger.apply(action_id="open:2026-10-12:44", side="buy",
                 contract=contract, price=Decimal("20"), quantity=1,
                 day="2026-10-12", now=NOW)
    report = read_paper_status(path)
    assert report["closed_trades"] == 0
    assert report["position"]["product_id"] == 44
    assert report["realized_pnl_inr"] == "0"
    ledger.apply(action_id="close:2026-10-12:44", side="sell",
                 contract=contract, price=Decimal("19"), quantity=1,
                 day="2026-10-12", now=NOW)
    report = read_paper_status(path)
    assert report["closed_trades"] == 1
    assert report["losses"] == 1
    assert report["wins"] == 0
    assert Decimal(report["realized_pnl_inr"]) == Decimal("-2.5075")
    assert report["real_orders_sent"] == 0
