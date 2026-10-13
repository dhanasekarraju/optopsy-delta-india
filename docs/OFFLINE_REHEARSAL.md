# Integrated offline exit-lifecycle rehearsal

Run `optopsy-delta rehearse-exits` without any API keys. It uses a synthetic
in-memory broker with deterministic orders and fills, and temporary SQLite
journals to test the integration between entry intents, exit proposals,
durable attempts and broker exit reconciliation.

The six scenarios are: (1) partially filled exit, restart, new unique exit
and verified flat inventory; (2) unknown broker acknowledgement permanently
blocking automatic retries; (3) unregistered/manual product fills blocking
reconciliation; (4) broker outage preserving unknown state; (5) pending
broker order remaining unsettled; and (6) unfunded account blocking entry
plus stale exit quote triggering attention.

Successful tests demonstrate only that **our software components work under
simulated responses**. No Delta order was sent, no real API response used,
and no production-order execution controller or exchange-side protection is
implemented. Real trading remains disabled, and the ₹5,000 cap is unchanged.

This command never asks for credentials, and it cannot make real orders.
