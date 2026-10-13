"""Example: run UNMODIFIED Optopsy on a MULTI-DAY Delta CSV.

Only usable when meaningful daily histories have accumulated; not intraday.
"""
from pathlib import Path
import optopsy as op

csv = Path("data/delta-daily.csv")
if not csv.exists():
    raise SystemExit("Run optopsy-delta export-daily first")

data = op.csv_data(str(csv), underlying_symbol=0, option_type=1, expiration=2,
                   quote_date=3, strike=4, bid=5, ask=6, delta=7)
print(op.long_calls(data))
