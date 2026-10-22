"""CN T03 named telemetry, without live accounts or vehicle commands."""
import asyncio
import copy
import dataclasses
import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
import command_client
from cn_backend import ChinaError
from cn_telemetry import decode_status
from test_cn_adapter import api, vehicle  # noqa: F401 — shared synthetic transport fixture


def t03():
    return dataclasses.replace(vehicle(), car_type='T03')


def named(shape='signalMap', **changes):
    signals = {'soc': 56, 'totalMileage': '2314', 'expectedMileage': 160,
               '2188': 0, '1879': 80, 'gearStatus': 0, 'speed': 0,
               'driverDoorLockStatus': True, 'bcmKeyPositionOn3': False,
               'bbcmBackDoorStatus': False, 'acSwitch': 'false',
               'indoorTemp': '0', 'minSingleTemp': 19,
               'batteryVoltage': 400, 'batteryCurrent': '-3.5', 'chargeState': 1,
               'leftFrontTirePressure': '253', 'rightFrontWindowPercent': 20,
               'longitude': 120.1, 'latitude': 30.1, 'collectTimeMs': 1_800_000_000_000}
    signals.update(changes)
    body = {'vin': t03().vin, 'collectTime': 1_800_000_001_000}
    body.update(signals if shape == 'data' else {shape: signals})
    return {'code': 0, 'data': body}


@pytest.mark.parametrize('shape', ['signalMap', 'signal', 'data'])
def test_named_t03_reads_real_values_and_preserves_missing_readings(shape):
    payload = named(shape)
    before = copy.deepcopy(payload)
    snapshot = decode_status(payload, t03())
    status = snapshot.status()
    assert (status['soc'], status['odometer_km'], status['range_km']) == (56, 2314, 160)
    assert status['gear'] == 'P' and status['speed_kmh'] == 0
    assert status['is_locked'] is True and status['ready'] is False
    assert status['climate_on'] is False and status['trunk_open'] is False
    assert status['inside_temp'] == 0 and status['battery_min_temp'] == 19
    assert status['charging'] == 1 and status['charge_current_a'] == -3.5
    assert status['charge_power_kw'] == 1.4
    assert status['tire_fl_bar'] == 2.53 and status['tire_rr_bar'] is None
    assert status['windows_open'] is True and status['seat_heat_driver'] is None
    assert status['frame_ts'] == 1_800_000_000_000
    assert status['latitude'] != 30.1 and status['longitude'] != 120.1  # GCJ02 → WGS84
    assert snapshot.raw_signals['soc'] == 56 and snapshot.raw_signals['2188'] == 0
    assert payload == before


def test_zero_soc_and_false_flags_are_real_readings():
    status = decode_status(named(soc=0, driverDoorLockStatus=False), t03()).status()
    assert status['soc'] == 0 and status['is_locked'] is False


@pytest.mark.parametrize('code,charging,plugged', [(0, 0, False), (1, 1, True), (2, 0, True),
                                                 (4, 0, True), (6, 0, True), (9, None, None)])
def test_named_t03_keeps_cn_charge_semantics(code, charging, plugged):
    status = decode_status(named(chargeState=code), t03()).status()
    assert status['charging'] == charging and status['plug_connected'] is plugged


def test_named_driving_regen_is_not_a_charge():
    status = decode_status(named(gearStatus=3, speed=30), t03()).status()
    assert status['charging'] == 0 and status['vehicle_state'] == 'driving'


def test_numbered_t03_still_reads_precise_soc_boolean_flags_and_gps_fallbacks():
    payload = {'code': 0, 'data': {'signalMap': {'100003': 62.5, '1204': 62, '1298': True,
        '1938': 'false', '1258': False, '3724': 120.1, '3725': 30.1, 'sts': 1_800_000_000}}}
    status = decode_status(payload, t03()).status()
    assert status['soc'] == 62.5 and status['is_locked'] is True
    assert status['climate_on'] is False and status['ready'] is False
    assert status['frame_ts'] == 1_800_000_000_000 and status['latitude'] is not None


def test_server_collection_time_does_not_advance_a_named_vehicle_frame():
    payload = named()
    first = decode_status(payload, t03())
    payload['data']['collectTime'] += 10000
    second = decode_status(payload, t03())
    assert first.data.timestamp_ms == second.data.timestamp_ms
    assert first.data.collect_time_ms != second.data.collect_time_ms
    payload['data']['signalMap'].pop('collectTimeMs')
    assert decode_status(payload, t03()).data.timestamp_ms == 0


def test_t03_frame_dates_use_china_timezone_and_reported_time_takes_priority():
    payload = named(collectTimeMs=None, collectTime='2026-10-20 08:09:10')
    expected = int(datetime(2026, 10, 20, 8, 9, 10, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
    assert decode_status(payload, t03()).data.timestamp_ms == expected
    payload['data']['signalMap']['sts'] = 1_800_000_000
    assert decode_status(payload, t03()).data.timestamp_ms == 1_800_000_000_000


def test_t03_collect_time_ms_can_sit_beside_the_named_map():
    payload = named()
    payload['data']['collectTimeMs'] = payload['data']['signalMap'].pop('collectTimeMs')
    assert decode_status(payload, t03()).data.timestamp_ms == 1_800_000_000_000
    payload['data']['collectTime'] += 10000
    assert decode_status(payload, t03()).data.timestamp_ms == 1_800_000_000_000


@pytest.mark.parametrize('modules,allowed', [((100, 200, 400), False), ((100, 200, 300, 400), True)])
def test_named_gps_still_requires_shared_location_permission(modules, allowed):
    car = dataclasses.replace(t03(), module_rights=modules)
    assert (decode_status(named(), car).status()['latitude'] is not None) is allowed


@pytest.mark.parametrize('bad_soc', [None, False, 'nan', 'inf', 'bad', -1, 101])
def test_invalid_soc_never_becomes_zero_or_a_saved_frame(bad_soc):
    with pytest.raises(ChinaError, match='沒有有效電量'):
        decode_status(named(soc=bad_soc), t03())


@pytest.mark.parametrize('payload', [None, [], {'code': 0, 'data': None},
    {'code': 40, 'data': {'soc': 62}}, {'code': 1023, 'data': {'soc': 62}},
    {'code': 0, 'data': {'vin': 'ANOTHER', 'soc': 62}},
    {'code': 0, 'data': {'signalMap': {'vin': 'ANOTHER', 'soc': 62}}},
    {'code': 0, 'data': {'signalMap': {'sts': 1_800_000_000_000}}}])
def test_bad_or_refused_responses_cannot_become_vehicle_data(payload):
    with pytest.raises(ChinaError): decode_status(payload, t03())


def test_named_compatibility_is_limited_to_t03():
    with pytest.raises(ChinaError, match='沒有有效電量'):
        decode_status(named(), vehicle())


def test_empty_map_placeholder_does_not_hide_named_data():
    payload = named('data')
    payload['data']['signalMap'] = None
    assert decode_status(payload, t03()).data.soc == 56


def test_named_frame_without_odometer_does_not_invent_zero_km():
    payload = named()
    payload['data']['signalMap'].pop('totalMileage')
    snapshot = decode_status(payload, t03())
    assert not snapshot.data.odometer_reported
    assert snapshot.status()['odometer_km'] is None


def wire_t03(api, monkeypatch, payload):
    car = t03()
    api._vehicles = {car.vin.lower(): car}
    transport = api._p.transport
    def answer(method, url, headers, body=None, form=False):
        if url.endswith('/signal/info/query'):
            api.calls.append((method, url, dict(headers), copy.deepcopy(body)))
            assert form is False and body == {'vin': car.vin}
            assert headers['cartype'] == 'T03'
            return payload
        return transport(method, url, headers, body, form)
    monkeypatch.setattr(api._p, 'transport', answer)
    session = command_client.LeapmotorSession()
    session._api, session._vehicle, session._vehicles = api, car, [car]
    monkeypatch.setattr(session, '_connect', lambda: None)
    monkeypatch.setattr(session, '_reset', lambda: None)
    monkeypatch.setattr(session, '_target', lambda: car)
    monkeypatch.setattr(command_client, '_session', session)
    return car, session


def test_poller_and_web_store_the_same_t03_reading(api, monkeypatch):
    import cn_backend
    import client
    import db as poller_db
    import db_reader
    car, session = wire_t03(api, monkeypatch, named())
    poller = object.__new__(client.LeapmotorMateClient)
    poller._api, poller._vehicle = api, car
    database = poller_db.Database(str(cn_backend.db_path()))
    vid = database.ensure_vehicle(car.vin, 'T03')
    database.save_position(vid, poller.get_status())
    monkeypatch.setattr(db_reader, 'DB_PATH', str(cn_backend.db_path()))
    monkeypatch.setattr(db_reader, '_conn_rw', lambda: database._conn)
    db_reader.save_fresh_signals(session.get_fresh_signals(car.vin), car.vin)
    rows = database._conn.execute('SELECT soc,odometer_km,range_km,charging,frame_ts,ready,inside_temp FROM positions ORDER BY id').fetchall()
    assert len(rows) == 2 and tuple(rows[0]) == tuple(rows[1])
    assert tuple(rows[0]) == (56, 2314, 160, 1, 1_800_000_000_000, 0, 0)
    assert sum(url.endswith('/signal/info/query') for _, url, _, _ in api.calls) == 2
    database._conn.close()


def test_diagnostics_keep_undecodable_raw_signals_without_saving_them(api, monkeypatch):
    import main
    import diagnostics
    payload = named(soc='unrecognized-value')
    car, session = wire_t03(api, monkeypatch, payload)
    monkeypatch.setattr(diagnostics.db_reader, 'get_vehicle', lambda: ({'vin': car.vin}, {}))
    assert session.get_fresh_signals(car.vin) is None
    response = asyncio.run(main.diagnostics_bundle(parts='signals'))
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        text = archive.read('leapmotor-mate-diagnostics.txt').decode()
    assert 'unrecognized-value' in text and 'totalMileage' in text
    assert 'latitude' not in text and 'longitude' not in text
    assert '30.1' not in text and '120.1' not in text
    assert 'no live signals' not in text


def test_diagnostics_never_returns_a_different_vehicles_signals(api, monkeypatch):
    payload = named()
    payload['data']['vin'] = 'ANOTHER'
    _, session = wire_t03(api, monkeypatch, payload)
    assert session.get_fresh_signals(diagnostic=True) is None
