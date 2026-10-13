"""Loopback-only, read-only Optopsy Delta status dashboard.

The HTTP service exposes only GET endpoints. It never imports the trading
transport, never creates broker journals, and cannot authorize an order.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .account_preflight import inspect_account
from .client import DeltaClient, SignatureError
from .existing_journals import existing_exit_status, existing_lifecycle_status
from .safety_watchdog import observe_safety
from .paper_reporting import read_paper_status

HOST = "0.0.0.0"  # Container-only; compose publishes to host LOOPBACK.
PORT = 8097

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Optopsy Delta | Safety Console</title>
<style>
:root{color-scheme:dark;font:15px system-ui,sans-serif;--bg:#0b1321;--panel:#132238;--line:#304259;--muted:#9db0c8;--text:#f0f5ff;--red:#ff9c9c;--accent:#86c4ff}
*{box-sizing:border-box}body{background:var(--bg);color:var(--text);margin:0;padding:2.3rem 1.2rem}
main{max-width:1060px;margin:auto}header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:1rem;align-items:center;margin-bottom:2rem}
h1{margin:.2rem 0;font-size:clamp(1.6rem,4vw,2.3rem)}h2{font-size:1rem;margin:0 0 1rem}
p{line-height:1.55}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.13em;font-size:.76rem}
.muted{color:var(--muted)}.tag{border:1px solid #8c4343;border-radius:100px;padding:.5rem .9rem;color:var(--red);font-size:.8rem;font-weight:800}
.layout{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1rem}
article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1.2rem}
.value{font-size:1.6rem;font-weight:800;margin-top:.3rem;overflow-wrap:anywhere}
.wide{margin-top:1rem}.warnings{margin:0;padding-left:1.25rem;line-height:1.85;color:var(--red)}
small{color:var(--muted)}.footer{margin-top:1.8rem;border-top:1px solid var(--line);padding-top:1rem}
button{background:#23486d;border:1px solid #5f8bb8;color:#fff;border-radius:8px;padding:.6rem 1rem;cursor:pointer}
button:focus-visible{outline:2px solid white}
@media(max-width:640px){body{padding:1rem}header{align-items:flex-start}}
</style></head><body><main>
<header><div><div class="eyebrow">DELTA INDIA / OPTOPSY</div>
<h1>Trading Safety Console</h1>
<p class="muted">Read-only broker observations — not an execution engine</p></div>
<div class="tag">LIVE ORDERS DISABLED</div></header>
<section class="layout" aria-label="Account summary">
<article><h2 class="muted">Private API</h2><div class="value" id="broker">Checking...</div></article>
<article><h2 class="muted">Open positions</h2><div class="value" id="positions">—</div></article>
<article><h2 class="muted">Active orders</h2><div class="value" id="orders">—</div></article>
<article><h2 class="muted">USD available</h2><div class="value" id="cash">—</div></article>
<article><h2 class="muted">Capital limit</h2><div class="value">₹5,000</div>
<small>Includes ₹500 reserved; no live deployment</small></article>
<article><h2 class="muted">Readiness</h2><div class="value" id="readiness">BLOCKED</div></article>
</section>
<article class="wide"><h2>Launch blockers / operator attention</h2>
<ul id="issues" class="warnings"><li>Loading status...</li></ul>
<p class="muted" id="journal">No trading journals created by this dashboard.</p></article>
<article class="wide"><h2>Paper strategy account (simulated)</h2>
<div class="layout"><div><small>Paper status</small><div class="value" id="paper-state">—</div></div>
<div><small>Paper cash ₹</small><div class="value" id="paper-cash">—</div></div>
<div><small>Closed trades</small><div class="value" id="paper-closed">—</div></div>
<div><small>Realized P&L ₹</small><div class="value" id="paper-pnl">—</div></div></div>
<p id="paper-position">Paper position: —</p><p id="paper-event" class="muted">—</p></article>
<article class="wide"><h2>Observed at</h2><p id="observed">—</p>
<p class="muted">This page refreshes every 30 seconds. Exchange snapshots are not atomic.
No alert delivery, autonomous exits, or broker-order protection is active.</p>
<button id="refresh" type="button">Refresh now</button></article>
<div class="footer muted">The ₹5,000 policy is a proposed cap, not a funded wallet.
No buy/sell, order-enable, API-key entry, or cancel controls are exposed.</div>
</main>
<script>
const ids=["broker","positions","orders","cash","readiness","issues","observed","journal"];
function set(id,value){document.getElementById(id).textContent=String(value)}
async function load(){
 try{
  const response=await fetch("/api/status",{cache:"no-store"});
  if(!response.ok)throw Error("Unavailable");
  const data=await response.json();
  set("broker",data.broker_healthy?"Connected":data.broker_status);
  set("positions",data.open_positions===null?"Unknown":data.open_positions);
  set("orders",data.active_orders===null?"Unknown":data.active_orders);
  set("cash",data.available_usd===null?"Not verified":data.available_usd);
  set("readiness","BLOCKED — NO LIVE ORDERS");
  set("observed",data.observed_at_utc);
  set("journal",data.journals_note);
  const paper=data.paper||{};
  set("paper-state",paper.status||"UNAVAILABLE");
  set("paper-cash",paper.cash_inr??"—");
  set("paper-closed",paper.closed_trades??"—");
  set("paper-pnl",paper.realized_pnl_inr??"—");
  set("paper-position",paper.position?"Paper position: "+paper.position.underlying+" "+paper.position.symbol+" × "+paper.position.quantity:"Paper position: flat");
  set("paper-event","Last paper observation: "+(paper.observed_at_utc||"none")+" / "+(paper.last_event||"none"));
  const warnings=document.getElementById("issues");
  warnings.replaceChildren();
  for(const issue of data.blockers){const li=document.createElement("li");li.textContent=issue;warnings.append(li)}
 }catch(_err){
  set("broker","Unavailable");set("positions","Unknown");set("orders","Unknown");
  set("cash","Not verified");set("readiness","BLOCKED");set("observed","Status unavailable");
  const node=document.getElementById("issues");
  node.replaceChildren();const li=document.createElement("li");
  li.textContent="Dashboard status request failed; account state is unknown.";node.append(li);
 }
}
document.getElementById("refresh").addEventListener("click",load);
load();setInterval(load,30000);
</script></body></html>"""


def _product_id() -> int | None:
    value = os.getenv("OPTOPSY_DASHBOARD_PRODUCT_ID", "")
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Invalid configured Delta product ID")
    return int(value)


def collect_status(client: DeltaClient | None = None) -> dict:
    """Collect authenticated GET observations. Failures are redacted and block."""
    now = datetime.now(UTC)
    product_id = _product_id()
    lifecycle = existing_lifecycle_status(os.getenv("OPTOPSY_LIFECYCLE_DB") or None)
    exits = existing_exit_status(os.getenv("OPTOPSY_EXIT_DB") or None)
    supplied = client if client is not None else DeltaClient()
    positions = orders = product = wallets = None
    broker_healthy = False
    reason = None
    if not supplied._key or not supplied._secret:
        reason = "CREDENTIALS_MISSING"
    else:
        try:
            wallets = supplied.wallet_balances()
            positions = supplied.open_positions()
            orders = supplied.active_orders()
            product = supplied.product_position(product_id) if product_id else None
            broker_healthy = True
        except SignatureError:
            reason = "CREDENTIALS_MISSING"
        except Exception:  # noqa: BLE001 - redact API failures, fail closed
            reason = None
    watchdog = observe_safety(
        now=now, broker_checked_at=now if broker_healthy else None,
        broker_healthy=broker_healthy,
        account_positions=positions, active_orders=orders,
        product_id=product_id, product_position=product,
        lifecycle_status=lifecycle, exit_status=exits,
        broker_unavailable_reason=reason,
    )
    blockers = set(watchdog["issues"])
    preflight = None
    if broker_healthy:
        try:
            preflight = inspect_account(wallets, positions, orders)
            blockers.update(preflight["blockers"])
            if preflight["status"] != "PRELIMINARY_CLEAR":
                blockers.add("ACCOUNT_PREFLIGHT_NOT_CLEAR")
        except Exception:  # noqa: BLE001 - invalid wallet evidence blocks
            blockers.add("WALLET_PREFLIGHT_INVALID")
    blockers.add("AUTONOMOUS_ENTRY_EXIT_CONTROLLER_NOT_RELEASED")
    blockers.add("INDEPENDENT_LIVE_SAFETY_AUDIT_PENDING")
    try:
        paper = read_paper_status(os.getenv("OPTOPSY_PAPER_DB", "data/paper-optopsy.sqlite"))
    except Exception:  # noqa: BLE001 - broken paper ledger never affects broker status
        paper = {"mode": "PAPER", "status": "REPORT_ERROR", "real_orders_sent": 0}
    return {
        "paper": paper,
        "mode": "READ_ONLY_DASHBOARD",
        "observed_at_utc": now.isoformat(),
        "broker_healthy": broker_healthy,
        "broker_status": ("CONNECTED" if broker_healthy else
                          "CREDENTIALS_MISSING" if reason else "UNKNOWN_OR_UNREACHABLE"),
        "open_positions": watchdog["account_position_rows"],
        "active_orders": watchdog["active_order_rows"],
        "available_usd": preflight["available_usd_equivalent"] if preflight else None,
        "capital_cap_inr": 5000, "reserve_inr": 500,
        "blockers": sorted(blockers),
        "journals_note": ("Only existing on-disk journals are read. "
                          "Missing bindings are never silently initialized."),
        "live_entry_authorized": False,
        "live_exit_authorized": False,
        "production_ready": False,
        "trading_enabled": False,
    }


def make_handler(provider: Callable[[], dict] = collect_status):
    """Factory allows offline tests to provide an inert fake status function."""
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content: bytes, mime: str):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            host = self.headers.get("Host", "").split(":", 1)[0].lower()
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            if self.path == "/healthz":
                self._send(200, b'{"server":"running","trade_enabled":false}',
                           "application/json; charset=utf-8")
            elif self.path == "/":
                self._send(200, HTML.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                try:
                    status = provider()
                    # Never present a report as ready, even if a fake provider
                    # accidentally sets a dangerous flag.
                    status.update(live_entry_authorized=False,
                                  live_exit_authorized=False,
                                  production_ready=False, trading_enabled=False)
                except Exception:  # noqa: BLE001 - redact all API/internal details
                    status = {
                        "mode": "READ_ONLY_DASHBOARD",
                        "observed_at_utc": datetime.now(UTC).isoformat(),
                        "broker_healthy": False,
                        "broker_status": "UNKNOWN_OR_UNREACHABLE",
                        "open_positions": None, "active_orders": None,
                        "available_usd": None,
                        "blockers": ["DASHBOARD_STATUS_UNAVAILABLE"],
                        "journals_note": "Status unavailable.",
                        "live_entry_authorized": False,
                        "live_exit_authorized": False,
                        "production_ready": False, "trading_enabled": False,
                    }
                output = json.dumps(status, sort_keys=True, allow_nan=False).encode()
                self._send(200, output, "application/json; charset=utf-8")
            else:
                self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self):
            self._send(405, b"Orders are not supported",
                       "text/plain; charset=utf-8")

        def log_message(self, format, *args):
            # Do not log paths, headers, tokens or account response data.
            return

    return Handler


def main() -> None:
    # Only intended for Docker service with 127.0.0.1 HOST publication.
    # Do not run with an externally published Docker port.
    ThreadingHTTPServer((HOST, PORT), make_handler()).serve_forever()


if __name__ == "__main__":
    main()
