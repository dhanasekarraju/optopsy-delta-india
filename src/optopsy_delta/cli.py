"""Read-only CLI; there is intentionally NO live order submission command."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .client import DeltaClient, DeltaError
from .snapshot import snapshot_to_rows, snapshots_to_daily_optopsy, write_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optopsy-delta",
                                     description="Optopsy Delta India read-only connector")
    cmds = parser.add_subparsers(dest="command", required=True)
    p = cmds.add_parser("capture", help="Capture exact public quote/product snapshot")
    p.add_argument("--underlying", choices=("BTC", "ETH"), required=True)
    p.add_argument("--directory", default="data/delta-snapshots")
    p = cmds.add_parser("export-daily", help="Daily-only Optopsy-compatible CSV (NOT intraday)")
    p.add_argument("--directory", default="data/delta-snapshots")
    p.add_argument("--output", required=True)
    p = cmds.add_parser("screen-calls", help="Read-only original Optopsy call candidates from snapshot")
    p.add_argument("--snapshot", required=True)
    cmds.add_parser("private-status", help="Read wallets, position count and open-order count; no writes")
    cmds.add_parser("account-preflight", help="Private read-only balance and exposure diagnostic")
    cmds.add_parser("rehearse-exits", help="Fully offline exit lifecycle rehearsal; no keys/orders")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        client = DeltaClient()
        # Verify product metadata is complete before recording.
        products = client.live_products()
        tickers = client.option_tickers(args.underlying)
        now = datetime.now(UTC)
        path = write_snapshot(args.directory, underlying=args.underlying,
                              captured_at=now, tickers=tickers, products=products)
        normalized = snapshot_to_rows(tickers, products, captured_at=now)
        print(f"Saved {path}; raw tickers={len(tickers)}, usable quotes={len(normalized)}")
        if not normalized:
            print("ERROR: zero usable quotes; capture preserved for diagnosis. No live data validation.")
            return 3
        return 0
    if args.command == "export-daily":
        paths = sorted(Path(args.directory).glob("*.json"))
        snapshots = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        df = snapshots_to_daily_optopsy(snapshots)
        df.to_csv(output, index=False)
        print(f"Exported {len(df)} daily contract quotes to {output}; NOT intraday evidence")
        return 0
    if args.command == "screen-calls":
        from .contract_selection import screen_snapshot_file

        print(json.dumps(screen_snapshot_file(args.snapshot), indent=2))
        return 0
    if args.command == "rehearse-exits":
        from .offline_rehearsal import run_rehearsal

        print(json.dumps(run_rehearsal(), indent=2))
        return 0
    if args.command == "account-preflight":
        from .account_preflight import account_preflight

        print(json.dumps(account_preflight(DeltaClient()), indent=2))
        return 0
    if args.command == "private-status":
        client = DeltaClient()
        balances = client.wallet_balances()
        positions = client.open_positions()
        orders = client.active_orders()
        # Don't print private holdings or credentials; operator can inspect exchange UI.
        print(json.dumps({"wallet_rows": len(balances),
                          "position_rows": len(positions) if isinstance(positions, list) else None,
                          "open_order_rows": len(orders)}))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeltaError as exc:
        raise SystemExit(f"Delta error: {exc}") from None
