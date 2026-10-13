# Broker-backed exit recovery (read-only)

Reconciles pending exit attempts against signed Delta order lookup, all
registered entry/previous exit orders, a complete paginated fill history
since the established flat-account baseline, current per-product inventory,
account-wide active orders and positions.

No POST, cancel, retry or trading-key activation is possible from this
module. Unknown broker outcomes remain unresolved. External fills, manual
trades, missing pages, inconsistent inventory and unrelated orders fail
closed; only matched terminal exit states can advance their journal.
Pending orders remain unresolved, even if partial fills are observed.

This is a sequence of broker GETs, **not an atomic exchange snapshot**.
It does not eliminate races, prove exclusive position ownership, or
implement an autonomous liquidation/watchdog. No live trading is authorized.
