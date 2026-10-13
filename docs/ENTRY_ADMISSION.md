# Entry admission — no real orders

The original Optopsy RSI-below-30 daily signal, fresh Delta quotes,
screened contract metadata, account preflight, watchdog report and
per-product flat inventory are checked together. Gateway tick, staleness,
depth and INR allocation validation is reused unchanged.

The planner produces a NON_EXECUTABLE_ENTRY_INTENT and cannot reserve,
submit or retry an order. It is not a live runner, and its inputs are not
an atomic broker snapshot. The original RSI function must run on a
compatible separate research runtime. No live permissions are granted.
