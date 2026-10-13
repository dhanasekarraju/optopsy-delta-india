"""Loopback-only, read-only Optopsy Delta status dashboard.

The HTTP service exposes only GET endpoints. It never imports the trading
transport, never creates broker journals, and cannot authorize an order.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .account_preflight import inspect_account
from .client import DeltaClient, SignatureError
from .existing_journals import existing_exit_status, existing_lifecycle_status
from .paper_reporting import read_paper_status
from .safety_watchdog import observe_safety

HOST = "0.0.0.0"  # Container-only; compose publishes to host LOOPBACK.
PORT = 8097

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Optopsy Delta | Control Room</title>
<style>
:root{color-scheme:dark;font:15px system-ui,-apple-system,Segoe UI,sans-serif;--bg:#0a111d;--panel:#122033;--panel2:#0f1a2b;--line:#2a3d57;--muted:#8fa4bf;--text:#eef4ff;--green:#4ade80;--amber:#fbbf24;--red:#ff8a8a;--blue:#7cc0ff;--grey:#7b8ba3}
*{box-sizing:border-box}body{background:radial-gradient(1200px 500px at 70% -10%,#16294a 0,var(--bg) 60%);color:var(--text);margin:0;padding:1.6rem 1rem 3rem}
main{max-width:1080px;margin:auto}
header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:1rem;align-items:flex-end;margin-bottom:1.4rem}
.eyebrow{color:var(--blue);font-weight:700;letter-spacing:.14em;font-size:.72rem}
h1{margin:.2rem 0 0;font-size:clamp(1.5rem,4vw,2.1rem)}h2{font-size:.78rem;letter-spacing:.09em;text-transform:uppercase;margin:0 0 .7rem;color:var(--muted)}
.muted{color:var(--muted)}p{line-height:1.5;margin:.3rem 0}
.pills{display:flex;gap:.5rem;flex-wrap:wrap}
.pill{border-radius:999px;padding:.4rem .85rem;font-size:.76rem;font-weight:800;border:1px solid var(--line);background:var(--panel2)}
.pill.green{color:var(--green);border-color:#1f6b3f}.pill.amber{color:var(--amber);border-color:#7a5b10}
.pill.red{color:var(--red);border-color:#8c4343}.pill.grey{color:var(--grey)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.9rem;margin-top:.9rem}
article{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:1.1rem 1.2rem}
.hero{display:flex;gap:1rem;align-items:center;border-left:5px solid var(--grey)}
.hero.green{border-left-color:var(--green)}.hero.amber{border-left-color:var(--amber)}.hero.red{border-left-color:var(--red)}
.big{font-size:1.45rem;font-weight:800;overflow-wrap:anywhere}.value{font-size:1.55rem;font-weight:800;overflow-wrap:anywhere}
.sub{font-size:.82rem;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:.86rem}th,td{text-align:left;padding:.5rem .4rem;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:.74rem;text-transform:uppercase;letter-spacing:.06em}
.tbl{overflow-x:auto}.buy{color:var(--green);font-weight:700}.sell{color:var(--amber);font-weight:700}
ul{margin:0;padding-left:1.2rem;line-height:1.8}ul.warn{color:var(--amber)}
details{margin-top:.7rem;color:var(--muted)}summary{cursor:pointer}
button{background:#1f4469;border:1px solid #5f8bb8;color:#fff;border-radius:8px;padding:.5rem .95rem;cursor:pointer}
button:focus-visible{outline:2px solid #fff}.row{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap;margin-top:1.2rem}
.wide{margin-top:.9rem}
</style></head><body><main>
<header><div><div class="eyebrow">DELTA INDIA / OPTOPSY</div><h1>Control Room</h1>
<p class="muted">Read-only view of the live worker and broker. This page cannot place orders.</p></div>
<div class="pills"><span class="pill grey" id="pill-mode">CHECKING</span><span class="pill grey" id="pill-beat">NO HEARTBEAT</span></div></header>

<article class="hero" id="hero"><div><h2>Live worker</h2><div class="big" id="live-state">—</div>
<p class="sub" id="live-detail">—</p></div></article>

<section class="grid" aria-label="Account">
<article><h2>Open positions</h2><div class="value" id="positions">—</div><div class="sub">at broker</div></article>
<article><h2>Active orders</h2><div class="value" id="orders">—</div><div class="sub">at broker</div></article>
<article><h2>USD available</h2><div class="value" id="cash">—</div><div class="sub" id="broker">—</div></article>
<article><h2>Capital limit</h2><div class="value">₹5,000</div><div class="sub">₹500 reserve, 1 contract at a time</div></article>
</section>

<article class="wide"><h2>Recent live orders</h2><div class="tbl"><table><thead><tr>
<th>Time (UTC)</th><th>Side</th><th>Market</th><th>Product</th><th>Filled</th><th>State</th></tr></thead>
<tbody id="orders-body"><tr><td colspan="6" class="muted">No live orders yet</td></tr></tbody></table></div></article>

<article class="wide"><h2>Needs attention</h2><ul id="issues" class="warn"><li>Loading...</li></ul>
<details><summary>Legacy checks (not used by the live worker)</summary><ul id="legacy"></ul></details></article>

<article class="wide"><h2>Paper account (simulated)</h2><div class="grid" style="margin-top:0">
<div><div class="sub">Status</div><div class="big" id="paper-state">—</div></div>
<div><div class="sub">Cash ₹</div><div class="big" id="paper-cash">—</div></div>
<div><div class="sub">Closed trades</div><div class="big" id="paper-closed">—</div></div>
<div><div class="sub">Realized P&amp;L ₹</div><div class="big" id="paper-pnl">—</div></div></div>
<p class="sub" id="paper-position">—</p></article>

<div class="row"><span class="sub" id="updated">—</span><button id="refresh" type="button">Refresh now</button></div>
<p class="sub">No buy/sell, key entry, or cancel controls exist here. Auto-refreshes every 30 seconds.</p>
</main>
<script>
const $=id=>document.getElementById(id);
function set(id,v){$(id).textContent=String(v)}
function pill(id,t,c){const n=$(id);n.textContent=t;n.className="pill "+c}
const FRIENDLY={HOLD:"Holding an open position",WAIT_NO_ENTRY_SIGNAL:"Waiting for an RSI entry signal",
WAIT_EXITED_THIS_SIGNAL_DAY:"Exited today; waiting for the next signal day",NO_ELIGIBLE_CALL:"Signal active, but no eligible call option",
ORDER_SUBMITTED:"Order sent to Delta",ORDER_NOT_CONFIRMED:"Order outcome unconfirmed; reconciling",
PENDING_ORDER_WAIT:"Waiting for an order to settle",ALREADY_ATTEMPTED:"Order for this signal already attempted",
ENTRY_BLOCKED_ACCOUNT_NOT_CLEAR:"Entry blocked: account is not clear",ENTRIES_PAUSED_BY_OPERATOR_FILE:"New entries paused (STOP_ENTRIES file)",
EXIT_DUE_QUOTE_UNAVAILABLE:"Exit is due but no usable quote yet",STOP_ERROR_NO_ACTION:"Error: no action taken",
RECOVERY_REQUIRED_RECONCILE_FAILED:"STOPPED: an order needs a manual check",LIVE_STANDBY_NOT_ENABLED:"Standby: live interlock is OFF",
TRADING_CREDENTIALS_MISSING:"Standby: trading keys missing"};
const LEGACY=["LIFECYCLE_JOURNAL_UNAVAILABLE","EXIT_JOURNAL_UNAVAILABLE","PRODUCT_NOT_BOUND","AUTONOMOUS_ENTRY_EXIT_CONTROLLER_NOT_RELEASED"];
const NOTES={BROKER_CREDENTIALS_MISSING:"Dashboard has no read-only key (optional). Add DELTA_INDIA_API_KEY to see account numbers here.",
INDEPENDENT_LIVE_SAFETY_AUDIT_PENDING:"No independent safety audit yet. Start small and watch the first trades.",
INSUFFICIENT_AVAILABLE_BALANCE:"Wallet is not funded yet."};
function age(iso){const t=Date.parse(iso);return t?Math.max(0,Math.round((Date.now()-t)/1000)):null}
function fmt(s){return s<90?s+"s ago":s<5400?Math.round(s/60)+"m ago":Math.round(s/3600)+"h ago"}
function renderLive(live){
 const ev=live.event||live.status||"NO_LIVE_HEARTBEAT",a=live.at?age(live.at):null;
 set("live-state",FRIENDLY[ev]||ev.replaceAll("_"," "));
 const bits=[];if(a!==null)bits.push("updated "+fmt(a));
 for(const k of["reason","symbol","side","journal_stage","error_type","detail"])if(live[k])bits.push(live[k]);
 set("live-detail",bits.join(" · ")||"The live worker has not reported yet.");
 let mode,mc,hc="grey";
 if(ev==="NO_LIVE_HEARTBEAT"||a===null){mode="WORKER NOT RUNNING";mc="grey"}
 else if(ev==="LIVE_STANDBY_NOT_ENABLED"||ev==="TRADING_CREDENTIALS_MISSING"){mode="LIVE OFF · STANDBY";mc="amber"}
 else if(a>180){mode="WORKER STALE";mc="red"}
 else if(/^(STOP_|RECOVERY_)/.test(ev)){mode="LIVE · NEEDS ATTENTION";mc="red"}
 else{mode="LIVE ARMED";mc="green"}
 pill("pill-mode",mode,mc);
 if(a===null)pill("pill-beat","NO HEARTBEAT","grey");else pill("pill-beat","HEARTBEAT "+fmt(a).toUpperCase(),a>180?"red":"green");
 $("hero").className="hero "+(mc==="grey"?"":mc);
}
function renderOrders(rows){
 const body=$("orders-body");body.replaceChildren();
 if(!rows||!rows.length){const tr=document.createElement("tr"),td=document.createElement("td");
  td.colSpan=6;td.className="muted";td.textContent="No live orders yet";tr.append(td);body.append(tr);return}
 for(const r of rows){const tr=document.createElement("tr");
  const state=r.resolved?"settled":"awaiting reconcile";
  const cells=[(r.created_at||"").replace("T"," ").slice(0,19),r.side,r.underlying,r.product_id,
   r.filled===null||r.filled===undefined?"—":r.filled,state];
  cells.forEach((v,i)=>{const td=document.createElement("td");td.textContent=String(v);
   if(i===1)td.className=r.side==="buy"?"buy":"sell";tr.append(td)});
  body.append(tr)}
}
function renderIssues(blockers){
 const a=$("issues"),l=$("legacy");a.replaceChildren();l.replaceChildren();
 const attention=(blockers||[]).filter(b=>!LEGACY.includes(b));
 if(!attention.length){const li=document.createElement("li");li.textContent="Nothing needs attention.";a.append(li)}
 for(const b of attention){const li=document.createElement("li");li.textContent=NOTES[b]||b;a.append(li)}
 for(const b of(blockers||[]).filter(b=>LEGACY.includes(b))){const li=document.createElement("li");li.textContent=b;l.append(li)}
}
async function load(){
 try{
  const response=await fetch("/api/status",{cache:"no-store"});
  if(!response.ok)throw Error("Unavailable");
  const d=await response.json();
  renderLive(d.live||{});renderOrders(d.live_orders||[]);renderIssues(d.blockers);
  set("positions",d.open_positions===null?"Unknown":d.open_positions);
  set("orders",d.active_orders===null?"Unknown":d.active_orders);
  set("cash",d.available_usd===null?"Not verified":d.available_usd);
  set("broker",d.broker_healthy?"Connected":d.broker_status);
  const p=d.paper||{};
  set("paper-state",p.status||"UNAVAILABLE");set("paper-cash",p.cash_inr??"—");
  set("paper-closed",p.closed_trades??"—");set("paper-pnl",p.realized_pnl_inr??"—");
  set("paper-position",p.position?"Paper position: "+p.position.underlying+" "+p.position.symbol+" × "+p.position.quantity:"Paper position: flat");
  set("updated","Updated "+new Date().toLocaleTimeString());
 }catch(_e){
  pill("pill-mode","DASHBOARD ERROR","red");set("live-state","Status unavailable");
  set("live-detail","The dashboard could not load status; account state is unknown.");
 }
}
$("refresh").addEventListener("click",load);load();setInterval(load,30000);
</script></body></html>"""


def _product_id() -> int | None:
    value = os.getenv("OPTOPSY_DASHBOARD_PRODUCT_ID", "")
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Invalid configured Delta product ID")
    return int(value)


def _read_live_orders(path: str, limit: int = 10) -> list[dict]:
    """Newest live order attempts, read-only. Any problem yields an empty list."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        try:
            rows = con.execute(
                "SELECT side,underlying,product_id,day,created_at,resolved,filled "
                "FROM attempts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []
    keys = ("side", "underlying", "product_id", "day", "created_at", "resolved", "filled")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def _read_live_heartbeat(path: str) -> dict:
    """Last live-worker result written by the worker; read-only, size-capped, never trusted."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(65536)
        obj = json.loads(raw)
    except FileNotFoundError:
        return {"status": "NO_LIVE_HEARTBEAT"}
    except Exception:  # noqa: BLE001
        return {"status": "LIVE_HEARTBEAT_UNREADABLE"}
    keep = ("event", "at", "reason", "symbol", "side", "error_type", "detail",
            "underlying", "journal_stage")
    return {k: str(obj[k])[:200] for k in keep if isinstance(obj, dict) and k in obj}


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
        "live_orders": _read_live_orders(
            os.getenv("OPTOPSY_LIVE_ATTEMPTS", "data/live-attempts.sqlite")),
        "live": _read_live_heartbeat(
            os.getenv("OPTOPSY_LIVE_HEARTBEAT", "data/live-heartbeat.json")),
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
