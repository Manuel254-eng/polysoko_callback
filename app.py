"""NaiPol M-Pesa callback relay.

Safaricom's Daraja API delivers results to public HTTPS URLs: STK Push
(deposit) results to the CallBackURL, B2C (withdrawal) results to the
ResultURL and expired B2C requests to the QueueTimeOutURL. This app is all
three: it acknowledges Safaricom at once (so Safaricom never times out or
retries) and forwards each callback, untouched, to the matching NaiPol
backend endpoint with a shared secret.

Nothing here decides an outcome — the backend verifies deposits with Daraja
before crediting, and holds any withdrawal it can't confirm for staff review.
A callback this relay fails to deliver is kept in SPOOL_FILE and can be
re-sent with `python app.py replay`.
"""

import json
import logging
import os
import sys
import threading
import time
from urllib.parse import urlsplit

import requests
from flask import Flask, abort, jsonify, request

ACK = {'ResultCode': 0, 'ResultDesc': 'Accepted'}
RETRY_DELAYS_SECONDS = (1, 2, 4, 8)
FORWARD_TIMEOUT_SECONDS = 10

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('mpesa-callback')


class Config:
    def __init__(self, env=os.environ):
        # The backend endpoint STK (deposit) callbacks are forwarded to.
        self.backend_callback_url = env['BACKEND_CALLBACK_URL']
        # B2C (withdrawal) endpoints — default to the same backend host.
        parts = urlsplit(self.backend_callback_url)
        origin = f'{parts.scheme}://{parts.netloc}'
        self.backend_b2c_result_url = env.get('BACKEND_B2C_RESULT_URL') or f'{origin}/api/wallet/mpesa/b2c/result/'
        self.backend_b2c_timeout_url = env.get('BACKEND_B2C_TIMEOUT_URL') or f'{origin}/api/wallet/mpesa/b2c/timeout/'
        # Sent as X-Callback-Secret; must match the backend's
        # SAFARICOM_DARAJA_CALLBACK_SECRET.
        self.forward_secret = env.get('FORWARD_SECRET', '')
        # Optional unguessable path segment: the CallBackURL becomes
        # https://<host>/mpesa/callback/<token>/ and anything else is a 404.
        self.path_token = env.get('CALLBACK_PATH_TOKEN', '')
        # Where callbacks that couldn't be delivered are kept, one per line.
        self.spool_file = env.get('SPOOL_FILE', 'undelivered_callbacks.jsonl')


def forward(config, url, payload):
    """POSTs `payload` to the backend `url`, retrying with backoff. Returns
    True once the backend has taken it."""
    headers = {'X-Callback-Secret': config.forward_secret} if config.forward_secret else {}
    for attempt, delay in enumerate((0, *RETRY_DELAYS_SECONDS), start=1):
        time.sleep(delay)
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=FORWARD_TIMEOUT_SECONDS)
            if resp.status_code < 500:
                if resp.status_code >= 400:
                    # The backend rejected it outright (e.g. a wrong secret) —
                    # retrying won't help, but keep it for a replay once fixed.
                    logger.error('Backend refused callback (%s): %s', resp.status_code, resp.text[:200])
                    return False
                return True
            logger.warning('Backend error %s on attempt %s', resp.status_code, attempt)
        except requests.RequestException as exc:
            logger.warning('Could not reach backend on attempt %s: %s', attempt, exc)
    return False


def spool(config, url, payload):
    with open(config.spool_file, 'a', encoding='utf-8') as spool_file:
        spool_file.write(json.dumps({'url': url, 'payload': payload}) + '\n')


def deliver(config, url, payload, label):
    if forward(config, url, payload):
        logger.info('Forwarded %s', label)
    else:
        spool(config, url, payload)
        logger.error('Could not forward %s — kept in %s', label, config.spool_file)


def stk_label(payload):
    """'STK callback <CheckoutRequestID>', or None if it isn't one."""
    try:
        return f"STK callback {payload['Body']['stkCallback']['CheckoutRequestID']}"
    except (TypeError, KeyError):
        return None


def b2c_label(payload):
    """'B2C <OriginatorConversationID>', or None if it isn't a B2C result."""
    try:
        result = payload['Result']
        return f"B2C {result.get('OriginatorConversationID') or result['ConversationID']}"
    except (TypeError, KeyError):
        return None


def create_app(config=None, background=True):
    config = config or Config()
    app = Flask(__name__)

    def relay(url, labeller, token):
        if token != config.path_token:
            abort(404)
        payload = request.get_json(silent=True)
        label = labeller(payload)
        if label is None:
            # Not the shape Safaricom sends to this URL.
            return jsonify({'ResultCode': 1, 'ResultDesc': 'Rejected'}), 400

        # Acknowledge first; the backend can be slow or briefly down.
        if background:
            threading.Thread(target=deliver, args=(config, url, payload, label), daemon=True).start()
        else:
            deliver(config, url, payload, label)
        return jsonify(ACK)

    routes = {
        'stk_callback': ('/mpesa/callback/', config.backend_callback_url, stk_label),
        'b2c_result': ('/mpesa/b2c/result/', config.backend_b2c_result_url, b2c_label),
        'b2c_timeout': ('/mpesa/b2c/timeout/', config.backend_b2c_timeout_url, b2c_label),
    }
    for name, (path, url, labeller) in routes.items():
        def view(token='', url=url, labeller=labeller):
            return relay(url, labeller, token)

        rule = f'{path}<token>/' if config.path_token else path
        app.add_url_rule(rule, name, view, methods=['POST'])

    @app.get('/healthz')
    def healthz():
        return jsonify({'ok': True})

    return app


def replay(config):
    """Re-sends every spooled callback; keeps the ones that still fail."""
    if not os.path.exists(config.spool_file):
        print('Nothing to replay.')
        return
    with open(config.spool_file, encoding='utf-8') as spool_file:
        entries = [json.loads(line) for line in spool_file if line.strip()]
    # Entries from before B2C support are bare STK payloads.
    entries = [entry if 'url' in entry else {'url': config.backend_callback_url, 'payload': entry} for entry in entries]
    failed = [entry for entry in entries if not forward(config, entry['url'], entry['payload'])]
    with open(config.spool_file, 'w', encoding='utf-8') as spool_file:
        for entry in failed:
            spool_file.write(json.dumps(entry) + '\n')
    print(f'Replayed {len(entries) - len(failed)} of {len(entries)}; {len(failed)} still undelivered.')


if __name__ == '__main__':
    if sys.argv[1:] == ['replay']:
        replay(Config())
    else:
        create_app().run(host='0.0.0.0', port=int(os.environ.get('PORT', '8080')))
else:
    # For gunicorn: `gunicorn app:app`.
    app = create_app() if 'BACKEND_CALLBACK_URL' in os.environ else None
