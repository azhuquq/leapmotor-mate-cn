"""Proactive account renewal must leave independent gateway sessions usable."""
import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_cn_adapter import api, gateway, jwt, vehicle
import cn_backend as cn
import crypto


def due(api, now, expires=1200):
    api._p.session.account_token_expires_at = now + expires if expires else 0
    api._save()


def peer(api):
    client = cn.ChinaAPIClient(username=api.username, transport=api._p.transport)
    client._vehicles = api._vehicles
    client._routes = dict(api._routes)
    return client


def wire_account(api, response=None, *, exchange=None, reject_gateway=False):
    original = api._p.transport
    calls = []
    def transport(method, url, headers, body=None, form=False):
        if '/getnewtoken?' in url:
            calls.append('account')
            if isinstance(response, Exception):
                raise response
            return response if response is not None else {'code': 200, 'data': {
                'token': 'synthetic-new-account', 'refreshToken': 'synthetic-new-refresh', 'tokenExpired': '21600'}}
        if reject_gateway and url.endswith('/token/v1/refresh'):
            calls.append('gateway')
            return {'code': 302010219}
        if url.endswith('/account/v1/login'):
            calls.append(('exchange', body['security']))
            return exchange if exchange is not None else gateway(jwt(byte=b'e'))
        return original(method, url, headers, body, form)
    api._p.transport = transport
    return calls


@pytest.mark.parametrize('remaining,expected', [(21600, 0), (1801, 0), (1800, 1), (1200, 1), (-60, 1), (0, 1)])
def test_account_renewal_window_and_unknown_expiry(api, monkeypatch, remaining, expected):
    now = cn.time.time()
    monkeypatch.setattr(cn.time, 'time', lambda: now)
    due(api, now, remaining)
    calls = wire_account(api)
    car_token, signing_key = api._p.session.car_token, api._p.session.hkdf_key
    api.get_vehicle_raw_status(vehicle())
    api.get_vehicle_raw_status(vehicle())
    assert calls.count('account') == expected
    assert api._p.session.car_token == car_token
    assert api._p.session.hkdf_key == signing_key
    restored = peer(api)
    restored.login()
    assert restored._p.session.token == ('synthetic-new-account' if expected else 'synthetic-legacy')
    assert restored._p.session.device_id == api.device_id
    assert calls.count('account') == expected


@pytest.mark.parametrize('failure', [cn.ChinaError('synthetic network unavailable'),
    {'code': 45}, {'code': 401}, {'code': 39}, {'code': 200, 'data': {}},
    {'code': 200, 'data': {'token': 'synthetic-partial', 'tokenExpired': '0'}},
    {'code': 200, 'data': {'token': 'synthetic-partial', 'tokenExpired': 'nan'}}])
def test_failed_account_renewal_keeps_gateway_and_throttle_after_restart(api, monkeypatch, failure):
    now = [cn.time.time()]
    monkeypatch.setattr(cn.time, 'time', lambda: now[0])
    due(api, now[0])
    before = copy.deepcopy(api._envelope()['session'])
    calls = wire_account(api, failure)
    assert api.get_vehicle_raw_status(vehicle())['code'] == 0
    assert api._envelope()['session'] == before
    restarted = peer(api)
    assert restarted.get_vehicle_raw_status(vehicle())['code'] == 0
    assert calls == ['account']
    assert restarted._account_refresh['retry_at'] == now[0] + cn.ACCOUNT_RETRY_MIN_SECONDS
    now[0] += cn.ACCOUNT_RETRY_MIN_SECONDS
    wire_account(restarted)
    assert restarted.get_vehicle_raw_status(vehicle())['code'] == 0
    assert restarted._p.session.token == 'synthetic-new-account'
    assert restarted._account_refresh['failures'] == 0


def test_retry_backoff_is_shared_and_bounded(api, monkeypatch):
    now = [cn.time.time()]
    monkeypatch.setattr(cn.time, 'time', lambda: now[0])
    due(api, now[0])
    calls = wire_account(api, cn.ChinaError('synthetic network unavailable'))
    for count, delay in enumerate([300, 600, 1200, 2400, 3600, 3600], 1):
        active = peer(api)
        assert active.get_vehicle_raw_status(vehicle())['code'] == 0
        assert calls.count('account') == count
        assert active._account_refresh['retry_at'] == now[0] + delay
        assert peer(api).get_vehicle_raw_status(vehicle())['code'] == 0
        assert calls.count('account') == count
        now[0] += delay


def test_risk_refusal_during_optional_renewal_stops_all_cloud_requests(api, monkeypatch):
    now = [cn.time.time()]
    monkeypatch.setattr(cn.time, 'time', lambda: now[0])
    due(api, now[0])
    calls = wire_account(api, {'code': 1023})
    with pytest.raises(cn.ChinaError) as error:
        api.get_vehicle_raw_status(vehicle())
    assert error.value.code == 1023
    assert not api.calls
    for elapsed in [0, 300, 599]:
        now[0] = api._account_refresh['last_attempt_at'] + elapsed
        with pytest.raises(cn.ChinaError):
            peer(api).get_vehicle_raw_status(vehicle())
    assert calls == ['account']
    assert not api.calls


def test_gateway_fallback_uses_proactively_renewed_account_once(api, monkeypatch):
    now = cn.time.time()
    monkeypatch.setattr(cn.time, 'time', lambda: now)
    due(api, now)
    api._apply_gateway(gateway(jwt(now - 1)))
    api._save()
    calls = wire_account(api, reject_gateway=True)
    assert api.get_vehicle_raw_status(vehicle())['code'] == 0
    assert calls == ['account', 'gateway', ('exchange', 'synthetic-new-account')]
    restored = peer(api)
    restored.login()
    assert restored._p.session.token == 'synthetic-new-account'
    assert restored._p.session.car_token == api._p.session.car_token


def test_failed_exchange_does_not_rollback_successful_account_renewal(api, monkeypatch):
    now = cn.time.time()
    monkeypatch.setattr(cn.time, 'time', lambda: now)
    due(api, now)
    old_car, old_key = api._p.session.car_token, api._p.session.hkdf_key
    calls = wire_account(api, reject_gateway=True, exchange={'code': 302010202})
    with pytest.raises(cn.ChinaAuthRequired):
        api.token_refresh()
    assert calls == ['account', 'gateway', ('exchange', 'synthetic-new-account')]
    assert api._p.session.token == 'synthetic-new-account'
    assert api._p.session.car_token == old_car and api._p.session.hkdf_key == old_key
    saved = json.loads(crypto.decrypt(cn.get_setting(cn.SESSION_KEY)))
    assert saved['session']['token'] == 'synthetic-new-account'
    assert saved['session']['car_token'] == old_car
    assert api._restore() and api._p.session.token == 'synthetic-new-account'


def test_failed_optional_renewal_does_not_repeat_in_same_gateway_fallback(api, monkeypatch):
    now = cn.time.time()
    monkeypatch.setattr(cn.time, 'time', lambda: now)
    due(api, now)
    calls = wire_account(api, {'code': 45}, reject_gateway=True)
    api.token_refresh()
    assert calls == ['account', 'gateway', ('exchange', 'synthetic-legacy')]
    assert api._p.session.token == 'synthetic-legacy'
    assert api._account_refresh['failures'] == 1


def test_account_refresh_forced_when_gateway_refused_but_account_not_due(api):
    calls = wire_account(api, reject_gateway=True)
    api.token_refresh()
    assert calls == ['gateway', 'account', ('exchange', 'synthetic-new-account')]


def test_concurrent_clients_renew_account_once(api):
    due(api, cn.time.time())
    calls = wire_account(api)
    second = peer(api)
    with ThreadPoolExecutor(2) as pool:
        assert [r['code'] for r in pool.map(lambda c: c.get_vehicle_raw_status(vehicle()), [api, second])] == [0, 0]
    assert calls == ['account']
    assert api._p.session.token == second._p.session.token == 'synthetic-new-account'


@pytest.mark.parametrize('failure', [False, True])
def test_processes_share_refresh_success_and_failure_lease(api, failure):
    due(api, cn.time.time())
    with cn.connect() as db:
        db.execute('CREATE TABLE synthetic_refresh_audit(count INTEGER)')
        db.execute('INSERT INTO synthetic_refresh_audit VALUES (0)')
    script = '''
import mate_api
import time
import cn_backend as cn
def transport(method,url,headers,body=None,form=False):
    assert '/getnewtoken?' in url
    with cn.connect() as db:
        db.execute('UPDATE synthetic_refresh_audit SET count=count+1')
    time.sleep(.15)
    if FAILURE:
        raise cn.ChinaError('synthetic network unavailable')
    return {'code':200,'data':{'token':'synthetic-process-account','tokenExpired':'21600'}}
api=cn.ChinaAPIClient(transport=transport)
api.login()
'''.replace('FAILURE', repr(failure))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'poller'))
    children = [subprocess.Popen([sys.executable, '-c', script], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stdout + stderr
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    with cn.connect() as db:
        assert db.execute('SELECT count FROM synthetic_refresh_audit').fetchone()[0] == 1


def test_new_sms_session_clears_old_lease_without_changing_active_session(api, monkeypatch):
    api._account_refresh = {'retry_at': cn.time.time() + 3600, 'failures': 5}
    api._save()
    active = cn.get_setting(cn.SESSION_KEY)
    def login(phone, code):
        api._p.session.phone = phone
        api._p.session.token = 'synthetic-sms-account'
        api._p.session.user_id = 'synthetic-sms-user'
        api._p.session.refresh_token = 'synthetic-sms-refresh'
        api._p.session.account_token_expires_at = int(cn.time.time()) + 21600
    monkeypatch.setattr(api._p, 'login', login)
    monkeypatch.setattr(api._p, 'car_login', lambda **kw: gateway())
    monkeypatch.setattr(api, '_list_vehicles', lambda **kw: [vehicle()])
    login_id, _ = api.verify_sms('13900000000', '123456')
    staged = json.loads(crypto.decrypt(cn.get_setting('cn_pending_' + login_id)))
    assert staged['account_refresh'] == {}
    assert staged['session']['token'] == 'synthetic-sms-account'
    assert cn.get_setting(cn.SESSION_KEY) == active
    assert api._restore()
    assert api._p.session.token == 'synthetic-legacy'
    assert api._account_refresh['failures'] == 5
