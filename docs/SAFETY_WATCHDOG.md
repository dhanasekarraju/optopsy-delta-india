# Safety watchdog (one-shot, read-only)

The safety watchdog evaluates a single observation of signed Delta
account-wide position and active-order GETs, a per-product current position
GET, lifecycle/exit-journal status, freshness and optional expiry/cutoff.

Unreachable broker API is UNKNOWN, **never flat**. It flags unresolved exit
acknowledgements, quarantined journals, active orders, foreign positions,
stale/invalid responses and open positions at expiry or pre-expiry cutoff.

The `SafetyIncidentJournal` persistently records status **transitions** in an
append-only SQLite table. It does not deliver push notifications, run in the
background, cancel orders, make protective sells, or prove exclusive position
ownership. Its outputs always prohibit trade authorization. The CLI below
makes one account check at the moment it is invoked and exits.

`optopsy-delta safety-status` is read-only and does NOT require keys.
It will report missing product binding/journals as ATTENTION_REQUIRED, rather
than claiming the unconfigured bot is ready. To inspect a particular
product with existing ledgers, operators must use a dedicated future
configuration/diagnostic; no automatic baseline setup is performed.
The optional private API key fields `DELTA_INDIA_API_KEY` and
`DELTA_INDIA_API_SECRET` are accepted only as environment variables. Never
put them in files or shell history.

This work does not justify funding or live activation. The watchdog is a
diagnostic, not an autonomous risk controller.
