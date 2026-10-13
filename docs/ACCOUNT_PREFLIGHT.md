# Authenticated read-only account preflight

The `account-preflight` CLI action requests signed wallet balances,
the account-wide margined positions, and all open/pending orders.
Private active orders are now exhaustively paginated, fail-closed with a
50-record page size, and refuse repeated cursors, duplicates, or missing metadata.

The diagnostic expects a USD-equivalent wallet, fixed Delta India ₹85/USD
conversion, and checks a maximum ₹5,000 project allocation, ₹500 reserve,
and provisional 5% fee buffer. Missing account or margin fields cause a
blocker, not a guessed zero. Any position, order, margin, or non-USD collateral
also blocks the preliminary clearance result.

Even `PRELIMINARY_CLEAR` is **not live approval**. The account-wide positions
endpoint may lag, wallet fields may differ in the real private response,
and owned-fill history, real-time position isolation, expiry policy and
automated protective exit remain unimplemented. The output ALWAYS sets
`live_entry_authorized` to false.

Never paste API secrets into chat. Do not create trading-enabled keys
for this diagnostic; separate read-data credentials can be supplied
privately via `DELTA_INDIA_API_KEY` and `DELTA_INDIA_API_SECRET`
only once the operator confirms applicable read scopes.
No change to any other VPS service or container.
