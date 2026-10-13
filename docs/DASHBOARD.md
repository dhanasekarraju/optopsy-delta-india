# Optopsy Delta safety dashboard (READ-ONLY)

Service: `compose.dashboard.yml`, dedicated Docker container
`optopsy_delta_dashboard`. It is published ONLY at host
`127.0.0.1:8097`. It does not alter the existing read-only
capture service or other trading stacks.

The server has only `GET /`, `GET /healthz`,
`GET /api/status`. HTTP POST always returns 405. It cannot
place, cancel, edit, schedule or retry exchange orders. UI always
says LIVE ORDERS DISABLED, and the API forces the trading status
flags to false. No API keys or positions are returned to the page
except the USD wallet's available numeric amount, position counts
and issue codes. Nothing is publicly exposed; use SSH port
forwarding for remote viewing.

To run with read-only private API access, supply Delta India
READ-ONLY credentials through a protected server-side environment
or terminal for the compose invocation (never share credentials
in the chat, expose them through browser fields, or commit them).
The container inherits the two private-read environment variables
only. NEVER pass DELTA_TRADE_API_KEY or DELTA_TRADE_API_SECRET.
Do not enable any live trading flags.

From VPS:
```sh
docker compose -f compose.dashboard.yml up -d --build
curl --silent http://127.0.0.1:8097/healthz
```

On laptop, SSH local tunnel:
```sh
ssh -N -L 8097:127.0.0.1:8097 ubuntu@YOUR_VPS_HOST
```
Then visit `http://127.0.0.1:8097`.

Before funded trading, a separate release must implement/verify
the actual order controller, protections, expiry settlement, and
an independent live-readiness audit. The dashboard cannot turn
the bot live. It is a one-shot GET collector refreshed by the
browser every 30 seconds, not an operational watchdog or alert.
