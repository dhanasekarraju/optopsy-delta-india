# Live worker (real orders)

`optopsy_delta.live_worker` runs the SAME unmodified Optopsy RSI strategy as the paper
worker (entry `rsi_below(14,30)`, exit `rsi_above(14,70)`, 0.30-delta screen, soonest expiry,
24h pre-expiry exit) but sends real Delta India IOC limit orders.

## Go-live checklist (operator steps; the code never does these for you)
1. Run paper for a while and compare behaviour. Keep `compose.paper.yml` data for reference.
2. Create a Delta India API key: TRADE permission only, NO withdrawal, whitelist the VPS IP.
3. Fund the account with the INR 5,000 allocation. Account must be flat with no open orders.
4. `docker compose -f compose.paper.yml down`
5. Set `paper_enabled = false` in `config.toml`.
6. `export DELTA_TRADE_API_KEY=... DELTA_TRADE_API_SECRET=... OPTOPSY_LIVE_ENABLED=YES_REAL_ORDERS`
7. `docker compose -f compose.live.yml up -d --build` and watch `docker logs -f optopsy_delta_live`.

## Controls
- Pause new entries (exits continue): `touch data/STOP_ENTRIES`; resume: delete it.
- Full stop: `docker compose -f compose.live.yml down`, or unset the env var. An open position is
  NOT auto-closed on stop; close it in the Delta UI.
- Heartbeat/last result: `data/live-heartbeat.json`. Order journal: `data/live-orders.sqlite`.
- `RECOVERY_REQUIRED_*`, `*_MANUAL_ACTION` events mean the bot stopped acting on purpose.

## Behaviour guarantees
- Broker position is the source of truth; unknown/multiple/short positions stop the bot.
- Entry only when the account is flat with no active orders and cost (+5% fee buffer) fits
  INR 5,000 minus INR 500 reserve. One contract, one position at a time.
- Unknown order outcomes are reconciled by client order ID, never blindly resubmitted.
- The pre-expiry exit does not depend on the signal/candle feed working.

## Known limits (read before funding)
- Not exercised against the real exchange; fake-exchange tests only. First trade: watch it.
- Exits are IOC at best bid: a thin book can leave a position unsold until a later cycle.
- Wallet INR value uses the fixed 85 USD/INR convention in this repo; verify with Delta.
- Fees/GST on the real fill are not reconciled against the fills feed yet.

## Older VPS CPUs
`constraints-cpu-compat.txt` pins NumPy 2.2.6 (baseline x86, no X86_V2 requirement) and the
tested pandas. If you see "NumPy was built with baseline optimizations", rebuild with
`docker compose -f compose.live.yml up -d --build` after pulling this branch.

## Changing capital later
Edit `.env` (section 5), then recreate BOTH containers so they re-read it:

    docker compose -f compose.live.yml up -d --force-recreate
    docker compose -f compose.dashboard.yml up -d --force-recreate

- `OPTOPSY_CAPITAL_INR`: budget ceiling (default 5000). Wallet must actually hold the money.
- `OPTOPSY_RESERVE_INR`: never spent (default 500).
- `OPTOPSY_MAX_CONTRACTS`: most contracts per entry (default 1). Raising capital alone does NOT
  buy more; size = min(this, visible ask depth, what the budget affords after a 5% fee buffer).
- Invalid values put the worker in standby (`CAPITAL_CONFIG_INVALID`); they never default upward.
- An open position is unaffected: exits always sell what the broker says you hold.
- The paper worker's simulated account stays at INR 5,000.
