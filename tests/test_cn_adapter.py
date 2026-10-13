"""China contracts exercised exclusively with synthetic sessions and fake transports."""
import base64
import copy
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import mate_api
from cn_backend import ChinaAPIClient, ChinaError, ChinaAuthRequired, UnsupportedFeature, origin, get_setting
from cn_telemetry import decode_status
from leapmotor_cn.protocol import Session
from leapmotor_cloud.mate_compat import Vehicle
from vehicle_snapshot import SnapshotSignals


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def jwt(expiry=None, byte=b'a'):
    return '.'.join((b64(b'{"alg":"synthetic"}'), b64(json.dumps({'exp': expiry or time.time() + 7200}).encode()), b64(byte * 32)))


def gateway(token=None):
    return {'code': 0, 'data': {'accessToken': token or jwt(), 'refreshToken': 'synthetic-refresh',
            'signParam': {'r2': base64.b64encode(b'b' * 32).decode(), 'r3': base64.b64encode(b'c' * 32).decode()}}}


def vehicle(vin='SYNTHETIC-B01', rights=None, modules=None):
    return Vehicle(vin, 'B01', True, year=2025,
        abilities=(1, 2, 3, 5, 10, 12, 13, 14, 21, 32, 34, 42, 50, 59, 68),
        rights=tuple(rights if rights is not None else (110, 120, 130, 160, 170, 190, 230, 240, 301, 320, 370, 440, 480)),
        module_rights=tuple(modules if modules is not None else (100, 200, 300, 400)))


def raw(**changes):
    signals = {'1204': 50, '1010': 0, '1319': 0, '1318': 100,
               '1149': 0, '1177': 400, '1178': -10, '1298': 1,
               'sts': int(time.time() * 1000), '1349': 25, '2646': 250}
    signals.update(changes)
    return {'code': 0, 'data': {'signalMap': signals, 'collectTime': int(time.time() * 1000)}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'mate.db'))
    monkeypatch.setenv('MATE_SECRET_KEY', 'synthetic-test-only')
    import crypto
    # The suite changes keys between fixtures; each process must use this fixture's key.
    monkeypatch.setattr(crypto, '_fernet', None)
    calls = []
    def transport(method, url, headers, body=None, form=False):
        calls.append((method, url, dict(headers), copy.deepcopy(body)))
        if url.endswith('/token/v1/refresh'): return gateway(jwt(byte=b'd'))
        if '/getCarRoute' in url: return {'code': 0, 'data': {'appRegion': 'https://appgateway.leapmotor.com'}}
        if url.endswith('/signal/info/query'): return raw()
        if '/verifyoperatepwdnew' in url: return {'code': 0}
        if '/appremotectl/query' in url: return {'code': 0, 'data': 1}
        if url.endswith('/appremotectl'): return {'code': 0, 'data': 'synthetic-message', 'timeout': 1}
        raise AssertionError('unexpected synthetic request path')
    client = ChinaAPIClient(username='13800000000', operation_password='1234', device_id='synthetic-device', transport=transport)
    client._p.session = Session(phone='13800000000', user_id='synthetic-account', device_id='synthetic-device',
        token='synthetic-legacy', refresh_token='synthetic-legacy-refresh', operate_pwd_mode='aes_jwt',
        account_token_expires_at=int(time.time()) + 21600)
    client._apply_gateway(gateway())
    client._save()
    car = vehicle()
    client._vehicles = {car.vin.lower(): car}
    client.calls = calls
    return client


def test_cn_session_is_encrypted_and_contains_no_pin(api):
    value = get_setting('cn_shared_session')
    assert value.startswith('enc:v1:')
    assert api._p.session.car_token not in value
    assert 'operate_pwd' not in api._envelope()['session']
    assert 'sms_code' not in api._envelope()['session']
    assert repr(api) == 'ChinaAPIClient(<private>)'


def test_two_clients_refresh_once_and_both_use_new_key(api):
    api._apply_gateway(gateway(jwt(time.time() - 1)))
    api._save()
    second = ChinaAPIClient(username=api.username, transport=api._p.transport)
    second._vehicles = api._vehicles
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(lambda client: client.get_vehicle_raw_status(vehicle()), (api, second)))
    assert sum('/token/v1/refresh' in call[1] for call in api.calls) == 1
    assert api._p.session.car_token == second._p.session.car_token
    assert api._p.session.hkdf_key == second._p.session.hkdf_key


def test_network_failure_does_not_replace_session_or_login(api):
    before = get_setting('cn_shared_session')
    def fail(*args): raise ChinaError('synthetic transport unavailable')
    api._p.transport = fail
    with pytest.raises(ChinaError): api.get_vehicle_raw_status(vehicle())
    assert get_setting('cn_shared_session') == before


def test_signs_exactly_the_headers_that_are_sent(api):
    api.get_vehicle_raw_status(vehicle())
    method, url, headers, body = api.calls[-1]
    signed = {key: headers[key] for key in ('acceptLanguage', 'channel', 'deviceType', 'source', 'timestamp', 'version', 'deviceId')}
    signed.update(body)
    expected = hmac.new(api._p.session.hkdf_key, ''.join(str(v) for _, v in sorted(signed.items())).encode(), hashlib.sha256).hexdigest()
    assert headers['sign'] == expected
    assert headers['x-region'] == 'CN'
    assert 'oversea' not in url


@pytest.mark.parametrize('bad', ['http://appgateway.leapmotor.com', 'https://leapmotor.com.evil.test', 'https://user@appgateway.leapmotor.com', 'https://appgateway.leapmotor.com:443', 'https://appgateway.leapmotor.com/path'])
def test_invalid_vehicle_route_rejected(bad):
    with pytest.raises(ChinaError): origin(bad)


@pytest.mark.parametrize('code,active,plugged', [(0,False,False),(1,True,True),(2,False,True),(3,False,True),(4,False,True),(5,False,False),(6,False,True),(9,False,False)])
def test_charging_uses_cn_state_not_current(code,active,plugged):
    snapshot = decode_status(raw(**{'1149':code,'1178':-50}),vehicle())
    assert bool(snapshot.data.charging_status) is active
    assert snapshot.data.plug_connected is plugged
    assert snapshot.data.charge_power_kw == 20
    assert snapshot.data.charge_completed is False
    if code == 9: assert 'charging_status' not in snapshot.reported_fields


def test_driving_regen_is_not_charging():
    data = decode_status(raw(**{'1010':3,'1319':60,'1149':1}),vehicle()).data
    assert data.vehicle_state == 'driving'
    assert data.charging_status == 0
    assert not data.plug_connected


def test_zero_soc_missing_values_and_rear_seat_collision():
    payload = raw(**{'1204':0,'1879':3,'1880':3,'3727':3,'3728':3})
    payload['data']['signalMap'].pop('1318')
    snapshot = decode_status(payload,vehicle())
    assert snapshot.data.soc == 0
    assert not snapshot.data.odometer_reported
    assert not snapshot.data.windows_open
    assert snapshot.status()['odometer_km'] is None
    assert snapshot.panel()['windows']['fl_pct'] is None
    assert snapshot.panel()['tyres']['fl']['bar'] == 2.5


def test_server_collection_time_never_creates_new_frame():
    payload = raw(**{'sts':1234567890123})
    first = decode_status(payload,vehicle())
    payload['data']['collectTime'] += 10000
    second = decode_status(payload,vehicle())
    assert first.data.timestamp_ms == second.data.timestamp_ms
    assert first.data.collect_time_ms != second.data.collect_time_ms


def test_location_permission_and_wgs_conversion():
    payload = raw(**{'3':30.1,'2':120.1})
    snapshot = decode_status(payload,vehicle())
    assert snapshot.data.latitude != 30.1
    assert snapshot.raw_signals['3'] == 30.1
    private = decode_status(payload,vehicle(modules=(100,200,400)))
    assert 'latitude' not in private.reported_fields
    assert private.status()['latitude'] is None


def test_unsupported_and_ungranted_commands_send_nothing(api):
    api._vehicles[vehicle().vin.lower()] = vehicle(rights=())
    with pytest.raises(UnsupportedFeature): api.lock_vehicle(vehicle().vin)
    with pytest.raises(UnsupportedFeature): api.set_climate_schedule(vehicle().vin, controls=[])
    with pytest.raises(UnsupportedFeature): api.send_destination(vehicle().vin)
    assert api.calls == []


def test_b01_scalar_receipt_is_accepted_and_pin_precedes_control(api):
    api.lock_vehicle(vehicle().vin)
    assert '/verifyoperatepwdnew' in api.calls[-2][1]
    assert '/app-control-service/' in api.calls[-1][1]
    form = api.calls[-1][3]
    assert json.loads(form['state']) == {'value':'lock'}
    assert form['oppwd'] != '1234'
    assert 'signStr' in form
    assert api.last_new_command_receipt.outcome == 'accepted'
    assert not api.last_new_command_receipt.physical_execution_confirmed


def test_wrong_pin_prevents_control(api):
    original = api._p.transport
    def wrong_pin(method,url,headers,body=None,form=False):
        if '/verifyoperatepwdnew' in url:
            api.calls.append((method,url,headers,body))
            return {'code':4}
        return original(method,url,headers,body,form)
    api._p.transport = wrong_pin
    with pytest.raises(ChinaError) as error: api.lock_vehicle(vehicle().vin)
    assert error.value.code == 4
    assert not any(url.endswith('/appremotectl') for _,url,_,_ in api.calls)


def test_window_and_climate_payloads_follow_cn_contract(api):
    api.windows(vehicle().vin,'10')
    assert json.loads(api.calls[-1][3]['state']) == {'value':'10'}
    api.ac_switch(vehicle().vin,params={'operate':'off'})
    body = json.loads(api.calls[-1][3]['state'])
    assert body['operate'] == 'off'
    assert set(body) == {'circle','mode','operate','position','temperature','windlevel','wshld'}


def test_pause_resume_then_stop_does_not_reopen_from_cable():
    from state_machine import StateMachine, State
    machine = StateMachine()
    for code in (1,6,1):
        machine.update(decode_status(raw(**{'1149':code}),vehicle()).data)
        assert machine.state == State.CHARGING
    machine.update(decode_status(raw(**{'1149':2}),vehicle()).data)
    assert machine.state != State.CHARGING
    machine.update(decode_status(raw(**{'1149':2}),vehicle()).data)
    assert machine.state != State.CHARGING


def test_poller_web_writers_share_values_and_source_semantics(api,monkeypatch):
    import db as poller_db
    import db_reader
    database = poller_db.Database(str(Path(get_setting.__globals__['db_path']())))
    vid = database.ensure_vehicle(vehicle().vin,'B01',2025)
    snapshot = decode_status(raw(**{'1149':2,'1178':-30}),vehicle())
    first = database.save_position(vid,snapshot.data)
    monkeypatch.setattr(db_reader,'DB_PATH',str(database._conn.execute('PRAGMA database_list').fetchone()[2]))
    monkeypatch.setattr(db_reader,'_conn_rw',lambda:database._conn)
    db_reader.save_fresh_signals(SnapshotSignals(snapshot),vehicle().vin)
    rows = database._conn.execute('SELECT charging,cn_charge_state,market,soc,frame_ts,window_fl_open FROM positions ORDER BY id').fetchall()
    assert tuple(rows[0]) == tuple(rows[1])
    assert rows[0]['charging'] == 0 and rows[0]['cn_charge_state'] == 2
    assert rows[0]['window_fl_open'] is None


def test_cn_unsupported_native_routes_do_not_connect(api,monkeypatch):
    import main as web_main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(web_main.command_client._session,'_connect',lambda:pytest.fail('unsupported route logged in'))
    client = TestClient(web_main.app)
    for path in ('/api/nav/send','/api/climate-schedule','/api/prepare-car/now'):
        response = client.post(path,json={})
        assert response.status_code == 409
        assert response.json()['unsupported']


def test_cn_market_setup_skips_certificates(api):
    import main as web_main
    from fastapi.testclient import TestClient
    client = TestClient(web_main.app)
    response = client.get('/setup')
    assert response.status_code == 200 and '短信登入' in response.text
    assert 'name="password"' not in response.text
    assert client.get('/api/setup/cert-status').json()['state'] == 'ready'


def test_sms_setup_commits_selected_cars_and_restores_without_sms(api, monkeypatch):
    import cn_backend
    import crypto
    import db as poller_db
    import db_reader
    import main as web_main
    from cn_capabilities import stored_permission
    from fastapi.testclient import TestClient

    path = str(cn_backend.db_path())
    database = poller_db.Database(path)
    monkeypatch.setattr(db_reader, 'DB_PATH', path)
    api.calls.clear()
    before = get_setting('cn_shared_session')
    def fake(method, url, headers, body=None, form=False):
        api.calls.append((method, url, headers, body))
        if '/sendmessagecode?' in url: return {'code':200}
        if '/check_login_with_phone?' in url:
            return {'code':200,'data':{'appLoginVO':{'token':'synthetic-account-token',
                'refreshToken':'synthetic-account-refresh','accountId':'synthetic-id','tokenExpired':'21600'}}}
        if url.endswith('/account/v1/login'): return gateway()
        if url.endswith('/vehicle/list'):
            car = vehicle()
            return {'code':0,'data':{'bindcars':[], 'sharedcars':[{'vin':car.vin,'carType':'B01','year':2025,
                'abilities':','.join(map(str,car.abilities)), 'rightList':','.join(map(str,car.rights)),
                'moduleRights':','.join(map(str,car.module_rights))}]}}
        return api._p.transport(method, url, headers, body, form)
    monkeypatch.setattr(cn_backend, 'http_request', fake)
    client = TestClient(web_main.app)
    assert client.post('/api/setup/sms/send',json={'phone':'+86 13800000000'}).status_code == 200
    # Server-side throttle persists across the short-lived HTTP client instances.
    assert client.post('/api/setup/sms/send',json={'phone':'13800000000'}).status_code == 400
    response = client.post('/api/setup/sms/verify',json={'phone':'13800000000','sms_code':'123456'})
    assert response.status_code == 200, response.text
    staged = response.json()
    assert staged['vehicles'] == [{'vin':vehicle().vin,'car_type':'B01','year':2025,'is_shared':True}]
    assert not any(key in staged for key in ('token','refreshToken','signParam'))
    assert get_setting('cn_shared_session') == before
    pending = get_setting('cn_pending_' + staged['login_id'])
    assert pending.startswith('enc:v1:') and '123456' not in crypto.decrypt(pending)
    car = {'vin':vehicle().vin,'battery':'56.0','pin':'1234','is_reev':False}
    invalid = client.post('/setup',data={'login_id':staged['login_id'],
        'vehicles_json':json.dumps([{**car,'vin':'UNLISTED'}])},follow_redirects=False)
    assert invalid.status_code == 400 and get_setting('cn_shared_session') == before
    saved = client.post('/setup',data={'login_id':staged['login_id'],'vehicles_json':json.dumps([car]),
        'timezone':'Asia/Hong_Kong'},follow_redirects=False)
    assert saved.status_code == 303
    assert db_reader.get_setting('setup_complete') == '1'
    assert db_reader.get_setting('language') == 'zh-TW'
    assert db_reader.get_setting('currency') == 'CNY'
    assert db_reader.get_setting('timezone') == 'Asia/Hong_Kong'
    assert db_reader.get_secret('leapmotor_pin_' + vehicle().vin.lower()) == '1234'
    assert db_reader.get_setting('is_reev_' + vehicle().vin.lower()) == '0'
    assert db_reader.get_vehicles()[0]['capacity_kwh'] == 56.0
    assert stored_permission(vehicle().vin,'healthy_charging',get_setting)
    assert not get_setting('cn_pending_' + staged['login_id'])
    api.calls.clear()
    restarted = ChinaAPIClient(username='13800000000', transport=fake)
    restarted.login()
    assert api.calls == []
    assert restarted.device_id == json.loads(crypto.decrypt(get_setting('cn_shared_session')))['session']['device_id']
    database._conn.close()


def test_charge_plan_preserves_fields_and_requires_positive_reply(api):
    api._configs[vehicle().vin] = (time.time(), {'isEnable':1,'percent':80,'beginTime':'23:00',
        'endTime':'07:00','cycles':'1,0,1,0,1,0,1','circulation':1,'recharge':0})
    api.set_charge_schedule(vehicle().vin, enabled=False,soc_limit=90,start_time='22:00')
    state = json.loads(next(c[3]['state'] for c in api.calls if c[1].endswith('/appremotectl')))
    assert state == {'chargeEnable':0,'chargesoc':90,'starttime':'22:00','endtime':'07:00',
        'cycles':'1,0,1,0,1,0,1','circulation':1,'recharge':0}
    assert api.last_new_command_receipt.outcome == 'confirmed'
    assert sum(c[1].endswith('/appremotectl') for c in api.calls) == 1


def test_risk_cooldown_blocks_all_new_requests(api):
    def rejected(*args):
        api.calls.append(args)
        return {'code':1023}
    api._p.transport = rejected
    with pytest.raises(ChinaError) as error: api.get_vehicle_raw_status(vehicle())
    assert error.value.code == 1023
    assert float(get_setting('cn_login_cooldown')) > time.time()
    count = len(api.calls)
    for action in (lambda:api.send_sms('13800000000'),lambda:api.get_vehicle_raw_status(vehicle()),
                   lambda:api.verify_sms('13800000000','123456')):
        with pytest.raises(ChinaError): action()
    assert len(api.calls) == count


def test_official_daily_empty_closed_day_is_zero_today_is_unknown(api):
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    from cn_backend import set_setting
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    yesterday = today - timedelta(days=1)
    api._routes[vehicle().vin] = 'https://appgateway.leapmotor.com'
    def empty(method,url,headers,body=None,form=False):
        api.calls.append((method,url,headers,body))
        return {'code':100}
    api._p.transport = empty
    rows = api.get_daily_energy(vehicle(),yesterday.isoformat(),today.isoformat())
    assert rows[0]['total_kwh'] == 0 and rows[0]['source'] == 'official'
    assert rows[1]['total_kwh'] is None and rows[1]['source'] == 'unavailable'
    api.calls.clear()
    assert api.get_daily_energy(vehicle(),yesterday.isoformat(),today.isoformat()) == rows
    assert api.calls == []
    # Cached history must not bypass a revoked shared-account module.
    api._vehicles[vehicle().vin.lower()] = vehicle(modules=(100,200,300))
    with pytest.raises(ChinaError) as error:
        api.get_daily_energy(vehicle().vin,yesterday.isoformat(),today.isoformat())
    assert error.value.code == 40 and api.calls == []


def test_saved_permissions_are_bound_to_account(api):
    from cn_backend import set_setting
    from cn_capabilities import stored_permission
    car = vehicle()
    caps = dict(is_shared=True,abilities=car.abilities,rights=car.rights,module_rights=car.module_rights,
        account=hashlib.sha256(api.username.encode()).hexdigest())
    set_setting('cn_capabilities_'+car.vin.lower(),json.dumps(caps))
    set_setting('leapmotor_user',api.username)
    assert stored_permission(car.vin,'lock',get_setting)
    set_setting('leapmotor_user','13900000000')
    assert not stored_permission(car.vin,'lock',get_setting)


def test_sql_charge_samples_exclude_stopped_and_driving_regen(api):
    import cn_backend
    import db as poller_db
    import db_reader
    database = poller_db.Database(str(cn_backend.db_path()))
    vid = database.ensure_vehicle(vehicle().vin,'B01',2025)
    for changes in ({'1149':1}, {'1149':2}, {'1149':6}, {'1149':1,'1010':3,'1319':60}):
        database.save_position(vid,decode_status(raw(**changes),vehicle()).data)
    rows = database._conn.execute('SELECT cn_charge_state,charging FROM positions WHERE '+db_reader._charging_sample()).fetchall()
    assert [tuple(row) for row in rows] == [(1,1)]
    database._conn.close()


def test_steering_heat_minutes_are_never_charge_time():
    payload = raw(**{'1624':15})
    snapshot = decode_status(payload,vehicle())
    assert 'remaining_charge_min' not in snapshot.reported_fields
    assert snapshot.status()['remaining_charge_min'] is None


@pytest.mark.parametrize('code', [39,302002002,302010205])
def test_signature_refusal_never_refreshes_or_replaces_session(api,code):
    before = get_setting('cn_shared_session')
    api._routes[vehicle().vin] = 'https://appgateway.leapmotor.com'
    def refused(method,url,headers,body=None,form=False):
        api.calls.append((method,url,headers,body))
        return {'code':code}
    api._p.transport = refused
    with pytest.raises(ChinaError) as error: api.get_vehicle_raw_status(vehicle())
    assert not isinstance(error.value,ChinaAuthRequired)
    assert len(api.calls) == 1
    assert get_setting('cn_shared_session') == before


def test_rejected_read_refreshes_once_then_retries_same_vin(api):
    api._routes[vehicle().vin] = 'https://appgateway.leapmotor.com'
    status_calls = 0
    original = api._p.transport
    def expired(method,url,headers,body=None,form=False):
        nonlocal status_calls
        if url.endswith('/signal/info/query'):
            status_calls += 1
            if status_calls == 1:
                api.calls.append((method,url,headers,body))
                return {'code':302002004}
        return original(method,url,headers,body,form)
    api._p.transport = expired
    assert api.get_vehicle_raw_status(vehicle())['code'] == 0
    assert status_calls == 2
    assert sum('/token/v1/refresh' in c[1] for c in api.calls) == 1
    assert all(c[3]['vin']==vehicle().vin for c in api.calls if c[1].endswith('/signal/info/query'))


def test_cn_locale_preserves_all_format_placeholders():
    import re
    root=Path(__file__).resolve().parent.parent/'web/locales'
    english=json.loads((root/'en.json').read_text())['translations']
    chinese=json.loads((root/'zh-TW.json').read_text())['translations']
    assert set(english)==set(chinese)
    for key,value in english.items():
        assert sorted(re.findall(r'\{[^}]+\}',value)) == sorted(re.findall(r'\{[^}]+\}',chinese[key])),key


def test_unknown_charge_is_unknown_in_snapshot_ui():
    snapshot=decode_status(raw(**{'1149':9}),vehicle())
    assert snapshot.status()['charging'] is None
    assert snapshot.status()['plug_connected'] is None


@pytest.mark.parametrize('method,key',[('quick_cool','boost_cool'),('quick_heat','boost_heat'),
                                     ('quick_vent','deodorize'),('windshield_defrost','defrost')])
def test_cn_climate_presets_match_reference_in_web_and_mqtt(api,method,key):
    from leapmotor_cn.protocol import LeapmotorClient
    getattr(api,method)(vehicle().vin)
    assert json.loads(api.calls[-1][3]['state']) == LeapmotorClient.AC_QUICK[key]


def test_ungranted_charge_plan_sends_no_config_or_control(api):
    api._vehicles[vehicle().vin.lower()] = vehicle(rights=())
    with pytest.raises(UnsupportedFeature):api.set_charge_limit(vehicle().vin,80)
    assert api.calls == []


def test_cn_blank_pin_clears_old_control_pin_and_logout_clears_all(api,monkeypatch):
    import cn_backend
    import db as poller_db
    import db_reader
    import main as web_main
    from fastapi.testclient import TestClient
    database=poller_db.Database(str(cn_backend.db_path()))
    monkeypatch.setattr(db_reader,'DB_PATH',str(cn_backend.db_path()))
    db_reader.set_secret('leapmotor_pin_'+vehicle().vin.lower(),'1234')
    web_main.apply_setup_vehicles([{'vin':vehicle().vin,'car_type':'B01','battery':'56','pin':''}])
    assert db_reader.get_secret('leapmotor_pin_'+vehicle().vin.lower()) == ''
    db_reader.set_secret('leapmotor_pin_second','5678')
    db_reader.set_setting('cn_capabilities_second','synthetic')
    client=TestClient(web_main.app)
    assert client.post('/api/account/logout').status_code == 204
    assert not get_setting('cn_shared_session')
    assert not db_reader.get_secret('leapmotor_pin_second')
    assert not get_setting('cn_capabilities_second')
    assert db_reader.get_vehicles()[0]['vin']==vehicle().vin
    database._conn.close()


def test_web_pin_selection_never_reuses_another_cars_pin(api,monkeypatch):
    import command_client,db_reader
    session=command_client.LeapmotorSession();session._api=api
    monkeypatch.setattr(db_reader,'get_operate_pin',lambda vin:'')
    session._use_pin_of('SECOND-CAR')
    assert api.operation_password == ''


def test_mqtt_discovery_signature_tracks_cn_rights_and_missing_comfort(api):
    from cn_backend import set_setting
    from mqtt import MqttService
    from types import SimpleNamespace
    car=vehicle()
    caps=dict(is_shared=True,abilities=car.abilities,rights=car.rights,module_rights=car.module_rights,
        account=hashlib.sha256(api.username.encode()).hexdigest())
    set_setting('leapmotor_user',api.username)
    set_setting('cn_capabilities_'+car.vin.lower(),json.dumps(caps))
    bridge=MqttService('synthetic',1883,get_setting=get_setting)
    granted=bridge._access_signature(car.vin)
    assert bridge._command_visible(car.vin,'climate_auto')
    assert bridge._command_visible(car.vin,'fan_level')
    caps['rights']=[]
    set_setting('cn_capabilities_'+car.vin.lower(),json.dumps(caps))
    assert bridge._access_signature(car.vin) != granted
    assert not bridge._command_visible(car.vin,'climate_auto')
    bridge.client=SimpleNamespace(is_connected=lambda:True,publish=lambda *args,**kwargs:None)
    bridge.publish_status(decode_status(raw(),car).data)
    assert bridge.climate_on_for(car.vin) is None


def test_unknown_charge_frame_does_not_close_an_existing_session():
    from state_machine import StateMachine,State
    machine=StateMachine()
    machine.update(decode_status(raw(**{'1149':1}),vehicle()).data)
    machine.update(decode_status(raw(**{'1149':9}),vehicle()).data)
    assert machine.state==State.CHARGING
    machine.update(decode_status(raw(**{'1149':2}),vehicle()).data)
    assert machine.state!=State.CHARGING


def test_every_supported_mqtt_climate_alias_has_cn_permission():
    from cn_capabilities import permission
    for action in ('climate_auto','climate_cool','climate_heat','climate_vent','climate_defrost',
                   'climate_off','fan_level','recirculation','climate_temperature'):
        assert permission(vehicle(),action)[0],action


def test_manual_sms_reauthentication_wakes_startup_wait_for_same_account(api,tmp_path,monkeypatch):
    import db as poller_db
    from poll_cycle_fixture import NOW,make_startup,poller_main
    module=poller_main('poller_cn_startup_retry')
    startup=make_startup(module,tmp_path,monkeypatch)
    original=poller_db.Database.set_setting
    def reauthenticated(db,key,value):
        original(db,key,value)
        if key=='last_loop_ts':original(db,'cn_shared_session','synthetic-new-login')
    monkeypatch.setattr(poller_db.Database,'set_setting',reauthenticated)
    client,database,clock=startup(1,error=ChinaAuthRequired('請以短信重新登入'))
    assert client.attempts == 2
    assert clock-NOW == 5
    database._conn.close()


def test_chinese_number_and_currency_formatting(monkeypatch):
    import units,db_reader,main as web_main
    monkeypatch.setattr(db_reader,'get_language',lambda:'zh-TW')
    monkeypatch.setattr(db_reader,'get_currency',lambda:db_reader.CURRENCIES['CNY'])
    assert units.decimal_point('50.0') == '50.0'
    assert web_main._dec(50,1) == '50.0'
    assert web_main._money(1234.5) == '¥1,234.50'
    assert web_main._price_3(0.25) == '0.250'


def test_missing_cn_readings_are_unknown_on_overview(api,monkeypatch):
    import cn_backend,db as poller_db,db_reader,main as web_main
    from fastapi.testclient import TestClient
    database=poller_db.Database(str(cn_backend.db_path()))
    vid=database.ensure_vehicle(vehicle().vin,'B01',2025)
    monkeypatch.setattr(db_reader,'DB_PATH',str(cn_backend.db_path()))
    payload=raw()
    for key in ('1318','1319','1010'):payload['data']['signalMap'].pop(key)
    database.save_position(vid,decode_status(payload,vehicle()).data)
    response=TestClient(web_main.app).get('/api/status-card')
    assert response.status_code==200
    assert 'leading-none mt-1">—' in response.text
    assert 'text-white text-lg">—' in response.text
    assert db_reader.get_latest_status()['odometer_km'] is None
    database._conn.close()
