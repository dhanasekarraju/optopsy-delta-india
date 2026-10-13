# Delta India VPS public read-only check

This is **not a live trading deployment**. It makes one public HTTPS GET capture per command, writes only under this repository's data directory, and uses no Delta API secrets. No strategies are tuned and no orders can be sent.

On the VPS:

~~~bash
mkdir -p ~/optopsy-delta-india
cd ~/optopsy-delta-india
git clone https://github.com/dhanasekarraju/optopsy-delta-india.git .
mkdir -p data
chmod 700 data
docker compose -f compose.readonly.yml build capture

OPTOPSY_UID=$(id -u) OPTOPSY_GID=$(id -g) docker compose -f compose.readonly.yml run --rm --no-deps capture capture --underlying BTC --directory data/delta-snapshots
OPTOPSY_UID=$(id -u) OPTOPSY_GID=$(id -g) docker compose -f compose.readonly.yml run --rm --no-deps capture capture --underlying ETH --directory data/delta-snapshots
~~~

To display **sanitized public-only metrics** instead of dumping entire snapshots:

~~~bash
python3 - <<'PY'
from pathlib import Path
import json
for f in sorted(Path('data/delta-snapshots').glob('*.json')):
    d=json.loads(f.read_text())
    print(f.name, 'underlying=', d.get('underlying'), 'products=', len(d.get('raw_products',[])), 'tickers=', len(d.get('raw_tickers',[])), 'usable_quotes=', len(d.get('normalized',[])))
PY
~~~

If a capture reports zero usable quotes, validation fails; its raw public snapshot remains available for diagnosis. **Do not paste Delta API keys, secrets, or private balances in chat.**

No host ports are published. This one-shot service neither restarts nor connects to Apex, ZeroBha, Nautilus or Freqtrade. The container is read-only except for its dedicated data mount.

A successful test establishes only Delta public API connectivity, product/ticker parsing and snapshot storage. It does not establish autonomous signal generation, 5-minute replay fidelity, profitability, exchange auth, fees, fills, safe exits, or a real-money ₹5,000 cap. No live order routing has been implemented.

## Older VPS CPU / NumPy compatibility

The public capture image intentionally installs no Pandas or NumPy, and the
CLI defers these imports until an optional daily research export. This avoids
NumPy wheel X86_V2 requirements on older VPS CPU models. If a previous image
failed at NumPy import, rebuild the image after pulling the fix, and inspect
its installed packages using `docker run --rm --entrypoint python
optopsy-delta-india:readonly -m pip list`. Do not modify VPS CPU flags, install
NumPy system-wide, or change existing trading containers.
