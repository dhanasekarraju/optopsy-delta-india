# Delta India opt-in live transport and original Optopsy signals

## What has been implemented

- `src/optopsy_delta/live_gateway.py`: code that can construct and HMAC-SHA256-sign Delta Exchange India `POST /v2/orders` **real limit orders** (IOC), with `client_order_id` <=32 characters, explicit `reduce_only` exits and read-only lookup by client ID. It has TWO independent interlocks: the transport is constructed with `live_enabled=True`, AND the environment equals `OPTOPSY_LIVE_ENABLED=YES_REAL_ORDERS`. Both are OFF by default.
- `LiveIntentJournal`: SQLite write-ahead order reservation, stable client IDs, replay conflict detection and no retry of ambiguous POST acknowledgements. Reconciliation GET remains available with the order kill-switch off. Acknowledged orders are *never treated as confirmed fills*.
- `validate()`: conservative inputs check BTC/ETH option entry allocation assumptions, valid size, limit price tick, recent exchange timestamps, quote spread, depth, ₹5,000 nominal allocation, ₹500 reserve and 5% provisional fee/rounding buffer (versus Delta India current 3.5% option premium fee cap plus 18% GST = 4.13%). The USD/INR conversion is *not verified* from Delta: caller must supply and validate it separately.
- `src/optopsy_delta/original_signals.py`: Fetches **fully closed daily** BTCUSD/ETHUSD candles and delegates RSI calculations directly to unmodified Optopsy `op.rsi_below(period=14, threshold=30)`, `op.rsi_above(period=14, threshold=70)` and `op.signal_dates()`. No RSI reimplementation or strategy tuning. It runs only where `optopsy==2.3.0`, Pandas, NumPy and TA dependencies work.
- A suite of deterministic unit tests uses fake network requests, **no real orders or keys**. No changes to Optopsy upstream files.

## What is NOT implemented and thus NOT safe to deploy as an autonomous live bot

- There is **no scheduled daily signal-to-execution loop**; the modules deliberately are NOT exposed as CLI live entry routes.
- No Delta-verified live product selection exactly matching Optopsy's default DTE and delta grouping and option expiration rules. The strategy template operates on entire historical option grids, and it does not itself choose a single live product.
- No binding from authenticated wallet asset balances to actual INR amount and exchange-enforced allocation/fees; `cash_inr`, `usd_inr`, tick and depth are **caller-provided** at this stage, so they cannot be treated as live safety guarantees.
- No private fill-ledger pagination, stateful partial fill/exit supervision, verified account position ownership, close-size reconciliation, post-disconnect recovery process, option-expiry handling or protective last-resort exit.
- No dashboard or production deployment. The CPU-incompatible VPS might fail to load unmodified Optopsy/Pandas; the order transport is kept separate from the heavy signal runtime.

**Do not set either live interlock to true or supply a Trading-enabled key yet.** This is usable source code for the broker integration and exact-original RSI signal adapter, **not** a completed automated live trading application. The 13 tests prove these narrow modules, not the finished bot.

## Safe verification

In an isolated Python 3.12+ environment, install repository dev dependencies and run:

```bash
python -m pytest -q tests/test_live_gateway.py tests/test_original_signals.py
python -m py_compile src/optopsy_delta/live_gateway.py src/optopsy_delta/original_signals.py
```

The signal adapter requires optional Optopsy dependencies and is not invoked unless called explicitly. Do not create a Trading-enabled Delta API key until the fully supervised order lifecycle is complete.

## Why the exact strategy cannot simply be switched on

Original Optopsy `long_calls` defines an options backtest construction using its default 0.30 target delta (0.20–0.40 band), max entry DTE=90 and exit DTE=0, with optional entry and exit filters. The explicitly selected and published entry/exit filters are RSI below 30 / above 70. The **specific choice of which eligible live expiry/strike/lot to buy**, order timing, positions and exit supervision are extra executable system policies, absent from that historical strategy function. Treat them as separately declared operational choices, not as unchanged Optopsy source behavior.

Official references: https://docs.delta.exchange/ (Orders, Get order by client oid, GET historical ohlc candles) and https://github.com/goldspanlabs/optopsy/blob/main/docs/entry-signals.md.

Fee buffer and minimum contract size must be revalidated against actual Delta India broker settings before live entry. The budget estimate remains unverified without authenticated wallet funds and ownership accounting. See https://www.delta.exchange/support/solutions?articleId=80001177864 and https://www.delta.exchange/support/solutions?articleId=80001200076.
