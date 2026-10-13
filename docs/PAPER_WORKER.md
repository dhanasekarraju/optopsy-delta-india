# Continuous Optopsy paper trading worker

The worker runs automatically every 60 seconds using ONLY public Delta
candles, live product metadata and tickers. It executes upstream
`optopsy==2.3.0` `rsi_below(14,30)` and `rsi_above(14,70)` without changing
either signal or the upstream module.

An additional *paper-only* operational choice selects the soonest eligible
expiry from the upstream per-expiry 0.30-delta candidates. Simulated buys use
observed best ask and simulated sells observed best bid. These are hypothetical
fills and can materially overestimate actual executable fills.

The worker simulates a single call contract at a time, starts with ₹5,000
fictional cash, reserves ₹500, estimates 5% fee per fill, and uses an assumed
85 INR/USD PAPER exchange conversion. A paper-only 24-hour pre-expiry exit
guards against indefinite expired positions. On missing data it does not
invent a fill. SQLite stores atomic state and immutable fill events; stop
and restart resumes its paper state.

The worker NEVER connects to a trading API account, cannot POST real orders,
and is not an unattended real-money system. The existing web dashboard
continues to show the real Delta account and does not yet display paper
inventory. The paper ledger lives at `data/paper-optopsy.sqlite`.

After CI is green, on VPS with a writable `data` directory owned by the
container UID:
```sh
docker compose -f compose.paper.yml up -d --build
docker logs --tail 30 optopsy_delta_paper
```

The host CPU's compatibility with the upstream Optopsy NumPy stack must
be verified. This build may not start on older x86 CPUs; do not deploy a
crashing service or fall back to a reimplemented RSI.
