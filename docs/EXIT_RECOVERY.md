# Exit-attempt recovery journal — no-trade component

The `exit_recovery.ExitAttemptJournal` accepts only previously validated
`DRAFT_EXIT_ONLY` proposals. It durably reserves a unique client ID compatible
with the existing Delta trading gateway, with an initial state of
`PREPARED_UNKNOWN`: the journal assumes a request **might** have been submitted
before a crash. There is **no API POST, retry, cancel or execution loop**.

Offline simulated broker responses are reconciled against order identity,
reduce-only side, terminal IOC state, registered fill IDs/quantities, current
per-product position, and account-wide active orders. An unknown broker order
cannot be declared absent: it remains `PREPARED_UNKNOWN`; a pending/open
order returns `WAIT_FOR_BROKER_SETTLEMENT`. Mismatches quarantine attempts,
including across restarts. A terminal partial or unfilled order may only
prepare a NEW distinct exit draft using a newer quote and the exact remaining
position. Flat inventory cannot create another exit attempt.

Important limitations and safety constraints:
- This journal does not perform authenticated broker reads itself; all broker
  records are injected and must be independently validated and complete.
- Positive broker inventory after timeout, power failure, or network outage
  is **not** automatically liquidated here. An exchange-side protective
  mechanism and a supervised order-control loop remain unfinished.
- The state is purposely insufficient to authorize a real buy or sell.
  `submit_authorized` always remains false.
- A correct exit quote and no active orders are necessary but insufficient
  evidence that the bot exclusively owns the position.
- No trading credentials, VPS daemon, live order path or original Optopsy
  strategy rules changed. This module remains isolated from the active
  `LiveIntentJournal` and `DeltaTradingTransport` execution path.

See `docs/EXIT_SUPERVISOR.md`, `docs/POSITION_LIFECYCLE.md` and
`docs/BROKER_LIFECYCLE.md` for the other independent prerequisites.
