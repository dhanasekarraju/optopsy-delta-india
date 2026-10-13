# Paper lifecycle foundation — not a live trader

This branch adds **simulated** long BTC/ETH call and put option entry/exit
accounting, not a strategy signal engine or a Delta order router.

- No changes to upstream Optopsy or any of its 38 strategy definitions.
- A future external signal source must supply signal ID, contract, contract
  multiplier, entry ask, exit bid and quote timestamp. `long_calls` and
  `long_puts` are labels, **not autonomously generated signals**.
- Ledger: separate SQLite file, serialized writer transactions, persistent
  position quantity, immutable journal, idempotent entry/exit IDs.
- ₹5,000 aggregate model budget, ₹500 reserved, 3% estimated fee buffer;
  these are **research assumptions**, not Delta-verified fee schedules,
  currency conversion, available balance or an exchange-enforced limit.
- Entries with stale quotes (>30 seconds), invalid data, duplicate conflicting
  IDs, unsupported option strategies or insufficient budget are rejected.
- Exits never exceed paper-held size and permit partial closes.
- Paper fills at ask/bid are hypothetical. There are no actual fills,
  stops, target exits, automatic strategies, authenticated account operations,
  dashboard, schedule, order POST/DELETE, or emergency broker liquidations.
- Test via `python -m pytest -q tests/test_paper_engine.py`.
- Must verify Delta India contract multiplier, tick size, lot size, fees,
  account balances, partial fills, idempotent client order IDs, read-after-
  failure reconciliation, reduce-only exit semantics and crash recovery
  before implementing any live execution.
