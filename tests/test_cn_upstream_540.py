"""The upstream 5.4.0 shared fields and command names retain CN semantics."""
import dataclasses

import pytest
import mate_api  # noqa: F401
import cn_backend
import cn_capabilities
import db
import db_reader
from cn_telemetry import decode_status
from test_cn_adapter import api, raw, vehicle  # noqa: F401


def test_missing_cn_tyre_readings_stay_unknown_in_shared_snapshot_and_storage(api, monkeypatch):
    monkeypatch.setattr(db_reader, 'DB_PATH', str(cn_backend.db_path()))
    database = db.Database(str(cn_backend.db_path()))
    car = vehicle()
    vid = database.ensure_vehicle(car.vin, car.car_type)
    snapshot = decode_status(raw(**{'2646': None}), car)
    for corner in ('fl', 'fr', 'rl', 'rr'):
        assert getattr(snapshot.data, f'tire_{corner}_bar') is None
        assert getattr(snapshot.data, f'tire_{corner}_alarm') is None
    database.save_position(vid, snapshot.data)
    db_reader.save_fresh_signals(__import__('vehicle_snapshot').SnapshotSignals(snapshot), car.vin)
    columns = [f'tire_{c}_{kind}' for c in ('fl', 'fr', 'rl', 'rr') for kind in ('bar', 'alarm')]
    rows = database._conn.execute('SELECT ' + ','.join(columns) + ' FROM positions').fetchall()
    assert len(rows) == 2
    assert all(all(row[c] is None for c in columns) for row in rows)
    database.close()


def test_cn_tyre_alarm_and_pressure_reach_both_writers_and_panel(api, monkeypatch):
    monkeypatch.setattr(db_reader, 'DB_PATH', str(cn_backend.db_path()))
    database = db.Database(str(cn_backend.db_path()))
    car = vehicle()
    vid = database.ensure_vehicle(car.vin, car.car_type)
    snapshot = decode_status(raw(**{'2646': 250, '2641': 1, '2653': 270, '2648': 0}), car)
    assert snapshot.panel()['tyres']['fl'] == {'bar': 2.5, 'low': True}
    assert snapshot.panel()['tyres']['fr'] == {'bar': 2.7, 'low': False}
    database.save_position(vid, snapshot.data)
    db_reader.save_fresh_signals(__import__('vehicle_snapshot').SnapshotSignals(snapshot), car.vin)
    rows = database._conn.execute('SELECT tire_fl_bar,tire_fl_alarm,tire_fr_bar,tire_fr_alarm FROM positions').fetchall()
    assert [tuple(row) for row in rows] == [(2.5, 1, 2.7, 0)] * 2
    assert db_reader.tyre_warning() == {'status': 'too_low', 'tyres': ['fl']}
    database.close()


@pytest.mark.parametrize('code,charging,plugged', [(1, 1, True), (2, 0, True), (6, 0, True)])
def test_upstream_charge_helpers_do_not_reinterpret_cn_t03_parked_in_n(code, charging, plugged):
    car = dataclasses.replace(vehicle(), car_type='T03')
    data = decode_status(raw(**{'1010': 2, '1319': 0, '1149': code}), car).data
    assert data.vehicle_state == 'parked' and data.gear == 'N'
    assert data.charging_status == charging and data.cable_connected is plugged


def test_new_mqtt_temperature_command_uses_cn_climate_permissions():
    assert cn_capabilities.permission(vehicle(), 'climate_temp')[0]
    assert not cn_capabilities.permission(vehicle(rights=[]), 'climate_temp')[0]
    assert not cn_capabilities.permission(vehicle(modules=[]), 'climate_temp')[0]
