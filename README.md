# Optopsy × Delta Exchange India (read-only connector)

**Status: RESEARCH/DATA CONNECTOR. NOT A LIVE AUTOTRADER.** No real orders can be sent by this repository. No changes to Optopsy's 38 strategies, formulas, defaults, or source code. This is a separate installable Python package targeting BTC/ETH options on **Delta Exchange India** only.

## Why this is separate

Upstream: https://github.com/goldspanlabs/optopsy — stable version **2.3.0** (AGPL-3.0-or-later). This repository depends on upstream optionally (`pip install -e '.[research]'`) and does **not** vendor or patch Optopsy. Keep both repositories' license obligations in mind when redistributing. The adapter only adds (1) Delta public option product/ticker reads, (2) optional signed **read-only** balance, position and order checks, (3) immutable source quote snapshots, (4) a clearly labeled *daily* Optopsy CSV export and (5) a non-executable INR 5,000 premium budget estimator.

**Important:** Optopsy currently normalizes trade quote timestamps to calendar dates. A 5-minute Delta options strategy cannot be evaluated faithfully with the existing Optopsy simulator. Do not present daily Optopsy results as a 5-minute intraday backtest.

## Safety and ₹5,000 cap

- The user budget is **₹5,000**; no deposits or automatic funding are required.
- `CapitalPolicy` refuses premium estimates beyond the budget less reserve. It is an offline estimator only, not a broker risk limit.
- Live orders are **disabled by construction** (no order POST/DELETE or trade loop). This is necessary: Optopsy does not generate autonomous live trading signals or supervise stop orders. We must not invent entry rules while claiming to preserve its strategies.
- No market orders, naked short options, credentials in files, account-wide liquidations, or automated conversions.
- Private checks use a separate read-only API key kept **only** in the environment. Never paste keys into chat or git.
- VPS isolation: do not use Apex, ZeroBha, Nautilus or Freqtrade directories, processes, databases or credentials.

## Install (VPS separate virtualenv, Python 3.12+)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[research]'
.venv/bin/optopsy-delta capture --underlying BTC
.venv/bin/optopsy-delta capture --underlying ETH
.venv/bin/optopsy-delta export-daily --output data/delta-daily.csv
.venv/bin/python examples/research_only.py
```

Or, for data collection only, `pip install -e .` (without Optopsy). No private keys necessary for public `capture`.

**Read-only account check** (optional): set `DELTA_INDIA_API_KEY` and `DELTA_INDIA_API_SECRET` using a secure environment and run `optopsy-delta private-status`; keys must lack trading permissions. No credentials are printed by the application.

### Output reliability

- The capture preserves complete raw public ticker and product JSON, UTC collection time and a normalized view in one immutable file (0600) per run.
- If metadata is absent, the adapter drops that quote instead of guessing the expiry or delta. Investigate any mismatch in product fields before analyzing results.
- A daily export keeps **only the last quote observed per product per UTC day**. It is not a tradable fill history and cannot reconstruct 5-minute exits, spread dynamics or latency.
- Delta REST API fields, contract multiplier and settlement rules need real account/exchange validation before expanding to execution.

### How to graduate to genuinely live trading (not implemented)

1. Obtain reliable **timestamp-preserving** tick/orderbook data and independent 5-minute replay. Never modify the original Optopsy strategies to fake intraday support.
2. Define exactly which **existing** strategy maps to real-time *entry* and *exit* events, without inventing signal parameters; require an explicit user decision for its live policy.
3. Implement separate long-only broker execution, exact contract-size calculation, post-order reconciliation by client order ID, partial fills, retries without duplicates, bracket exits and crash recovery.
4. Enforce real-time available funds and ₹5,000 hard allocation separately from strategy calculations; test rejects, disconnects, stale feeds, gaps, errors, restart and fills.
5. Independently verify sandbox/paper behavior before any opt-in enabling of real orders.

## Development

```bash
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests
```

## Official API references

- https://docs.delta.exchange/ — India API base `https://api.india.delta.exchange/v2`.
- https://www.delta.exchange/support/solutions?articleId=80001200076 — fixed USD/INR exchange convention (documented as 85; may change).

## License

Separate adapter authored under AGPL-3.0-or-later to facilitate compliance with the original project's terms. See https://www.gnu.org/licenses/agpl-3.0.html for full license text.
