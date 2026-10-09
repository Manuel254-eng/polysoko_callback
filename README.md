# NaiPol M-Pesa callback relay

The public HTTPS endpoints Safaricom's Daraja API sends results to:
deposit (STK Push) results, and withdrawal (B2C) results and timeouts. It
acknowledges Safaricom immediately and forwards each callback, unchanged, to
the matching NaiPol backend endpoint with a shared secret.

| Relay path (give these to Daraja) | Forwarded to |
|---|---|
| `/mpesa/callback/<token>/` | `/api/wallet/mpesa/callback/` (deposits) |
| `/mpesa/b2c/result/<token>/` | `/api/wallet/mpesa/b2c/result/` (withdrawal results) |
| `/mpesa/b2c/timeout/<token>/` | `/api/wallet/mpesa/b2c/timeout/` (withdrawals that expired in Safaricom's queue) |

It never decides an outcome: the backend confirms every deposit with Daraja
before crediting, and holds any withdrawal it can't confirm for staff review.
A callback the relay can't deliver is kept in the spool file for replay.

```
Safaricom ──POST──▶ relay /mpesa/.../<token>/ ──POST + X-Callback-Secret──▶ backend /api/wallet/mpesa/...
          ◀── {"ResultCode": 0} (immediately)
```

## Configure

Copy `.env.example` to `.env` (or set these in your host's dashboard):

| Variable | Required | What it is |
|---|---|---|
| `BACKEND_CALLBACK_URL` | yes | The backend's callback endpoint, e.g. `https://backend.naipol.com/api/wallet/mpesa/callback/` |
| `BACKEND_B2C_RESULT_URL`, `BACKEND_B2C_TIMEOUT_URL` | no | Default to `/api/wallet/mpesa/b2c/result/` and `/timeout/` on the same host |
| `FORWARD_SECRET` | recommended | Sent as `X-Callback-Secret`; must match the backend's `SAFARICOM_DARAJA_CALLBACK_SECRET` |
| `CALLBACK_PATH_TOKEN` | recommended | Unguessable path segment; requests to any other path get a 404 |
| `SPOOL_FILE` | no | Where undeliverable callbacks are kept (default `undelivered_callbacks.jsonl`) |
| `PORT` | no | Default `8080` |

Then point the backend at the relay, in the backend's `.env`:

```
SAFARICOM_DARAJA_CALLBACK_URL=https://<relay-host>/mpesa/callback/<CALLBACK_PATH_TOKEN>/
SAFARICOM_DARAJA_B2C_RESULT_URL=https://<relay-host>/mpesa/b2c/result/<CALLBACK_PATH_TOKEN>/
SAFARICOM_DARAJA_B2C_TIMEOUT_URL=https://<relay-host>/mpesa/b2c/timeout/<CALLBACK_PATH_TOKEN>/
SAFARICOM_DARAJA_CALLBACK_SECRET=<same value as FORWARD_SECRET>
```

## Run

Docker (any VPS, Render, Railway, Fly.io, ...):

```
docker build -t naipol-mpesa-callback .
docker run -d --env-file .env -p 8080:8080 naipol-mpesa-callback
```

Without Docker:

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a && . ./.env && set +a
gunicorn app:app --bind 0.0.0.0:8080 --workers 2 --threads 4
```

Safaricom only calls HTTPS URLs, so put it behind TLS (your host's HTTPS, or
nginx/Caddy with a certificate). `GET /healthz` returns `{"ok": true}` for
uptime checks.

## Replay undelivered callbacks

```
python app.py replay
```

## Test

```
pip install -r requirements-dev.txt
pytest
```

Simulate Safaricom against a running relay:

```
curl -X POST https://<relay-host>/mpesa/callback/<token>/ -H 'Content-Type: application/json' \
  -d '{"Body":{"stkCallback":{"MerchantRequestID":"1","CheckoutRequestID":"ws_CO_TEST","ResultCode":0,"ResultDesc":"ok"}}}'
```
