# Broker fill reconciliation foundation

This adds **read-only** signed Delta India per-product position GET and
paginated `GET /v2/fills`, with bounded pagination and duplicate detection.

- `DeltaClient.product_position(product_id)`: Delta real-time single-product
  read, rather than relying on the potentially lagged margined position list.
- `DeltaClient.fills_for_product(product_id,start_time_us,end_time_us)`:
  use true Unix **microsecond** bounds; read each fill-history page and
  fail if cursors repeat, records duplicate, product changes, metadata is
  malformed or safety page cap is reached. The caller must choose a time
  interval covering the complete order lifetime.
- `order_fill_status(order,fills,position)` compares order quantity minus
  unfilled quantity against exact order-specific fill records. It **never**
  treats a successful POST acknowledgment alone as filled, and it does not
  infer bot ownership merely because a broker position exists.

Still missing: full fills/fee ledger and account-wide history, manual/external
trade isolation, confirmed owned position tracking, exact asset conversion,
safe exit orders under partial fills and restart, scheduled native Optopsy
signal evaluations, and server-independent signal runtime.

The existing live-enabled interlocks remain disabled. No deployment or
live credential changes are made by this branch.
