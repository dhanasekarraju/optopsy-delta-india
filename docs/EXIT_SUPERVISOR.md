# Draft-only exit supervisor (no trade execution)

The pure-Python exit supervisor uses a successful **consistent-but-not-authorized**
registered-fill reconciliation, the unmodified Optopsy RSI-above-70 signal
from the last fully closed UTC day, a separately supplied explicit UTC
pre-expiry safety cutoff and a fresh matching best-bid quote to draft
a long-call **reduce-only IOC limit SELL**.

- RSI below/above rules are unchanged. The pre-expiry safety cutoff is a
  distinct *operational rule*, **not** an Optopsy strategy rule. The eventual
  live operator must explicitly choose it before it can be configured.
- Flat positions yield FLAT. Expired positions require urgent manual
  settlement reconciliation. Missing/stale daily signal blocks rather than
  silently HOLDing the position. An expiry guard can trigger without an RSI
  signal. Missing quote, stale quote, wrong product, bad tick or no bid depth
  yields EXIT_DUE_BLOCKED; it does not invent an executable price.
- When top-of-book displayed depth is below remaining inventory, the draft
  size is at most that displayed depth. Top-of-book depth is **not** a fill
  guarantee and no order is routed.
- The SQLite exit draft journal writes one immutable candidate with an
  at-most-once digest. On restart it cannot 'retry' or modify the proposal;
  a changed price or inventory requires human reconciliation and new
  lifecycle work. Neither this component nor its tests submit orders.
- The existing broker lifecycle report explicitly does *not* prove
  exclusive account ownership. These drafts are NOT sufficient grounds to
  turn on Delta trading keys, to place orders, or to operate unattended.
  The live exchange-order supervisor, retry/timeout and partial-fill
  execution cycle, exchange-side protection, watchdog/alerting, and
  settlement handling still require implementation and independent tests.

No Docker service was added; no existing VPS service was modified.

Offline illustration (no API):

\`\`\`python
from datetime import datetime, timedelta, UTC
from optopsy_delta.exit_supervisor import assess_exit
now = datetime.now(UTC)
report = dict(status="CONSISTENT_BUT_NOT_AUTHORIZED", consistent=True,
              live_order_authorized=False, exclusive_ownership_proven=False,
              product_id=44, reported_position_size=1,
              registered_bought=1, registered_sold=0)
signal = dict(underlying="BTC", strategy="long_calls",
              last_complete_utc_day=(now.date()-timedelta(days=1)).isoformat(),
              entry=False, exit=True, parameters_tuned=False,
              exit_rule="optopsy.rsi_above(period=14, threshold=70)")
quote = dict(product_id=44, best_bid="20.0", best_ask="20.5",
             tick_size="0.1", best_bid_size=1,
             timestamp_unix=int(now.timestamp()))
print(assess_exit(reconciliation=report, underlying="BTC",
                  now=now, expiry=now+timedelta(days=2),
                  safety_cutoff=now+timedelta(days=1),
                  original_signal=signal, quote=quote)["status"])
# DRAFT_EXIT_ONLY (NOT SENT)
\`\`\`
