# Registered-order position lifecycle and restart recovery

The isolated position_lifecycle module compares registered order identities
against settled broker orders, every fill for a product since a known flat
baseline, and the current per-product position. It detects partial IOC fills,
partial closes, unregistered/manual fills, missing history, active orders,
duplicate IDs, mismatched net inventory, and tampering or disappearing fills
across restarts. Any discrepancy makes its separate SQLite journal permanently
QUARANTINED, pending explicit human investigation.

Important limitations: this is a **local reconciliation component**, not a
broker-backed execution supervisor. It currently consumes supplied dictionaries
and has no authenticated network integration. It cannot certify that a broker
fill-history request is exhaustive, or that another trading system did not
operate before the baseline. Its `live_order_authorized` is ALWAYS false.
No mechanism to rescue/close a real position exists here yet.

Safety: no order submission, no trading credentials, no change to existing
Docker services. Baseline must be explicitly registered for one product after
an independently confirmed flat account (account-wide position API may lag).
Do not use this journal to enable live orders.
