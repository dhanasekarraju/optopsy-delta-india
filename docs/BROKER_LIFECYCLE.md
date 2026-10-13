# Read-only broker to lifecycle bridge

`broker_lifecycle.reconcile_broker` links previously registered
`LiveIntentJournal` orders to authenticated broker GET responses:
single-order lookup by immutable broker ID, all account-wide positions,
all active orders, complete paged fill history for the baseline interval,
and a real-time single-product position read.

The bridge stops if the lifecycle baseline is absent/quarantined, any
registered intent is ambiguous/rejected, order IDs differ, another
position or pending order is present, or fill records disagree. A successful
result is ALWAYS `CONSISTENT_BUT_NOT_AUTHORIZED`.

**Important:** An existing intent journal is not yet a complete registry of
all trades executed by other software or manually; merely matching
registered fills to broker positions cannot independently prove ownership.
The general position list can lag. The bridge must not be used as
the gate for automatically placing orders or for autonomous exits.
There is no CLI hook, background service or order submission here.

No real Delta credentials or real orders are used in the deterministic tests.
