"""Paper trade accounting tests, no live exchange or Optopsy strategy writes."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from optopsy_delta.paper_engine import PaperError, PaperLedger

T = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


def offer(signal="a", **overrides):
    args = dict(signal=signal, strategy="long_calls", underlying="BTC", product_id=123,
                size=1, ask="20", contract="0.01", quoted_at=T, now=T)
    return args | overrides


def test_open_close_restart_and_append_only(tmp_path):
    path = tmp_path / "paper.db"
    p = PaperLedger(path)
    assert p.enter(**offer())["state"] == "PAPER_ENTERED"
    assert p.status()["cash_estimate_inr"] == "4982.4900"
    assert p.status()["mode"] == "PAPER"
    assert PaperLedger(path).status()["open_positions"][0]["remaining"] == 1
    assert p.exit(exit_id="x", signal="a", size=1, bid="25",
                  quoted_at=T, now=T)["state"] == "PAPER_EXITED"
    assert p.status()["open_positions"] == []
    assert p.status()["events"] == 2
    with sqlite3.connect(path) as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM events")


def test_entry_and_exit_retry_no_double_mutation(tmp_path):
    p = PaperLedger(tmp_path / "paper.db")
    p.enter(**offer())
    assert p.enter(**offer(now=T + timedelta(minutes=1)))["state"] == "ALREADY_RECORDED"
    with pytest.raises(PaperError, match="Conflicting"):
        p.enter(**offer(ask="21"))
    p.exit(exit_id="one", signal="a", size=1, bid="15", quoted_at=T, now=T)
    assert p.exit(exit_id="one", signal="a", size=1, bid="15",
                  quoted_at=T, now=T)["state"] == "ALREADY_RECORDED"
    with pytest.raises(PaperError, match="Conflicting"):
        p.exit(exit_id="one", signal="a", size=1, bid="16", quoted_at=T, now=T)
    assert p.status()["events"] == 2


def test_partial_exit_and_oversell(tmp_path):
    p = PaperLedger(tmp_path / "paper.db")
    p.enter(**offer(size=2))
    p.exit(exit_id="first", signal="a", size=1, bid="12", quoted_at=T, now=T)
    assert p.status()["open_positions"][0]["remaining"] == 1
    with pytest.raises(PaperError, match="exceeds"):
        p.exit(exit_id="second", signal="a", size=2, bid="12", quoted_at=T, now=T)


@pytest.mark.parametrize("changes", [
    {"strategy": "short_calls"}, {"strategy": "iron_condor"}, {"underlying": "SOL"},
    {"product_id": -1}, {"product_id": True}, {"size": -1}, {"size": True},
    {"ask": "NaN"}, {"ask": "-1"}, {"contract": "0"},
    {"signal": "../no"}, {"quoted_at": T - timedelta(minutes=2)},
    {"quoted_at": T + timedelta(seconds=5)},
])
def test_bad_entries_fail_closed(tmp_path, changes):
    p = PaperLedger(tmp_path / "paper.db")
    with pytest.raises(PaperError):
        p.enter(**offer(**changes))
    assert p.status()["events"] == 0


def test_cap_aggregated_across_entries(tmp_path):
    p = PaperLedger(tmp_path / "paper.db")
    p.enter(**offer("a", ask="3000"))
    with pytest.raises(PaperError, match="capital"):
        p.enter(**offer("b", ask="3000"))
    assert p.status()["events"] == 1


def test_concurrent_overspend_rejected(tmp_path):
    db = tmp_path / "paper.db"
    PaperLedger(db)

    def attempt(name):
        try:
            return PaperLedger(db).enter(**offer(name, ask="3000"))["state"]
        except PaperError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        actual = list(pool.map(attempt, ["a", "b"]))
    assert sorted(actual) == ["PAPER_ENTERED", "REJECTED"]


def test_missing_entry_cannot_exit(tmp_path):
    p = PaperLedger(tmp_path / "paper.db")
    with pytest.raises(PaperError, match="Unknown"):
        p.exit(exit_id="x", signal="none", size=1, bid="5", quoted_at=T, now=T)
