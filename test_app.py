import json
from unittest.mock import MagicMock, patch

import pytest

import app as relay

CALLBACK = {'Body': {'stkCallback': {
    'MerchantRequestID': 'mr-1', 'CheckoutRequestID': 'ws_CO_1', 'ResultCode': 0, 'ResultDesc': 'ok',
}}}


@pytest.fixture
def config(tmp_path):
    return relay.Config({
        'BACKEND_CALLBACK_URL': 'https://backend.example/api/wallet/mpesa/callback/',
        'FORWARD_SECRET': 's3cret',
        'CALLBACK_PATH_TOKEN': 'tok123',
        'SPOOL_FILE': str(tmp_path / 'spool.jsonl'),
    })


@pytest.fixture(autouse=True)
def no_sleep():
    with patch('app.time.sleep'):
        yield


def client(config):
    return relay.create_app(config, background=False).test_client()


def backend_response(status_code):
    return MagicMock(status_code=status_code, text='')


@patch('app.requests.post', return_value=backend_response(200))
def test_acknowledges_and_forwards_with_the_secret(post, config):
    resp = client(config).post('/mpesa/callback/tok123/', json=CALLBACK)

    assert resp.get_json() == relay.ACK
    post.assert_called_once()
    assert post.call_args.args[0] == config.backend_callback_url
    assert post.call_args.kwargs['json'] == CALLBACK
    assert post.call_args.kwargs['headers'] == {'X-Callback-Secret': 's3cret'}


@patch('app.requests.post')
def test_wrong_path_token_is_a_404(post, config):
    assert client(config).post('/mpesa/callback/wrong/', json=CALLBACK).status_code == 404
    post.assert_not_called()


@patch('app.requests.post')
def test_non_callback_bodies_are_rejected(post, config):
    assert client(config).post('/mpesa/callback/tok123/', json={'hello': 1}).status_code == 400
    post.assert_not_called()


@patch('app.requests.post', side_effect=[backend_response(502), backend_response(200)])
def test_retries_a_backend_error(post, config):
    client(config).post('/mpesa/callback/tok123/', json=CALLBACK)
    assert post.call_count == 2


@patch('app.requests.post', side_effect=relay.requests.ConnectionError('down'))
def test_undeliverable_callbacks_are_spooled_and_replayable(post, config):
    resp = client(config).post('/mpesa/callback/tok123/', json=CALLBACK)
    assert resp.get_json() == relay.ACK  # Safaricom is still acknowledged
    with open(config.spool_file) as spool_file:
        assert [json.loads(line) for line in spool_file] == [{'url': config.backend_callback_url, 'payload': CALLBACK}]

    post.side_effect = None
    post.return_value = backend_response(200)
    relay.replay(config)
    with open(config.spool_file) as spool_file:
        assert spool_file.read() == ''


def test_healthz(config):
    assert client(config).get('/healthz').get_json() == {'ok': True}


B2C_RESULT = {'Result': {
    'ResultType': 0, 'ResultCode': 0, 'OriginatorConversationID': 'WDR-1', 'ConversationID': 'AG_1',
}}


@pytest.mark.parametrize('kind', ['result', 'timeout'])
@patch('app.requests.post', return_value=backend_response(200))
def test_b2c_callbacks_go_to_their_backend_endpoints(post, config, kind):
    resp = client(config).post(f'/mpesa/b2c/{kind}/tok123/', json=B2C_RESULT)

    assert resp.get_json() == relay.ACK
    assert post.call_args.args[0] == f'https://backend.example/api/wallet/mpesa/b2c/{kind}/'
    assert post.call_args.kwargs['json'] == B2C_RESULT
    assert post.call_args.kwargs['headers'] == {'X-Callback-Secret': 's3cret'}


@patch('app.requests.post')
def test_b2c_route_rejects_an_stk_payload(post, config):
    assert client(config).post('/mpesa/b2c/result/tok123/', json=CALLBACK).status_code == 400
    post.assert_not_called()


@patch('app.requests.post', return_value=backend_response(200))
def test_replay_handles_spool_lines_from_before_b2c(post, config):
    with open(config.spool_file, 'w') as spool_file:
        spool_file.write(json.dumps(CALLBACK) + '\n')
    relay.replay(config)
    assert post.call_args.args[0] == config.backend_callback_url

