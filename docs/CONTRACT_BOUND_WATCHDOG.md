# Contract-bound read-only watchdog

Run `optopsy-delta safety-status --product-id PRODUCT_ID` to inspect a
specific contract's real-time position alongside account-wide private reads.
Optionally pass `--lifecycle-db /path/to/existing.sqlite` and
`--exit-db /path/to/existing.sqlite` to observe existing journals. Neither
path is initialized, created, upgraded, or reset by this command.

If either journal is absent, inaccessible, or malformed the watchdog reports
an unavailable/untrusted journal. Do NOT create a baseline from a single
apparently empty account observation: the account-wide positions endpoint can
lag. Expiry and its operational cutoff remain unconfigured, and any exposed
position without that configuration is an alert.

This is a one-time read-only diagnostic, **not** an automatic order
controller, and passing it would not certify readiness to trade.
