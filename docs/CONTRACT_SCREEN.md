# Optopsy-contract candidate screen (public snapshot only)

This module builds a **per-expiry, read-only** list of Delta India BTC/ETH
long-call candidates from captured public market metadata. It makes no API
calls and cannot submit orders.

The following use unchanged Optopsy original defaults:
- `long_calls` filters option type to calls.
- `min_bid_ask=0.05`, requiring bid **and** ask strictly greater than 0.05.
- Default `leg1_delta` target 0.30, min 0.20, max 0.40.
- `abs(delta)` closest to 0.30 **within each expiry**; ties resolve to
  the lowest strike, matching `_select_closest_delta`.
- Upstream `max_entry_dte=90`, `exit_dte=0`, `dte_interval=7`.
  Its current DTE bucketing uses `range(0, 90, 7)` and right-closed
  `pd.cut`: therefore the last bucket ends at **84**, not 90.
  This module mirrors that edge rather than hiding the upstream behavior.

Additional broker-facing screening checks (not Optopsy strategy parameters):
known live/operational status, positive contract multiplier/tick size,
valid tick-size-aligned market quotes, unambiguous product IDs, positive strike,
and complete UTC settlement timestamps. These only narrow candidates;
they do not create extra trading signals.

**The original simulator does not select a single expiry.** This screen
deliberately returns one eligible call per expiry and labels every candidate
`live_order_authorized=false`. Selecting one live expiry and choosing
frequency/order timing will require a separately declared operational policy,
not a claim that Optopsy specified it.

Test on the VPS using existing captured immutable BTC snapshot:

```bash
cd ~/optopsy-delta-india
docker compose -f compose.readonly.yml build capture
docker run --rm --entrypoint python optopsy-delta-india:readonly -c 'import optopsy_delta.contract_selection; print("CONTRACT SCREEN IMPORT OK")'
```

`optopsy-delta screen-calls --snapshot PATH` uses a captured JSON snapshot in
the container's accessible filesystem and prints candidates; it does
not place orders, use authenticated keys, or choose an expiry.

Sources: original `optopsy/strategies/_helpers.py`, `filters.py`,
`types.py` and Delta India's `GET /v2/products` API documentation.
