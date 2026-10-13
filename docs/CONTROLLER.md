# Integrated controller: NONTRADING observation cycle

`controller_cycle` coordinates account, durable order-intent/lifecycle/exit
journals, authenticated Delta GET reconciliation, fresh watchdog checks,
and original Optopsy signal metadata through the existing entry and exit gates.

It returns one of: STOP, RECOVERY_REQUIRED, HOLD, FLAT, ENTRY_CANDIDATE_NOT_SENT,
EXIT_DRAFT_NOT_SENT. It never submits, cancels, retries or registers exchange
orders, and it is not scheduled by the VPS. Its external signal and
contract are still caller-supplied, not generated/verified by a running
original Optopsy evaluator. Status includes `production_ready=false`.

This is incremental coordination work, not an execution release.
Live remains OFF. Further mandatory work includes source-bound signal
and contract selection, independently attested currency/account exposure,
atomic admission around order submission, persistent worker scheduling,
full outage/expiry recovery and external audit.
