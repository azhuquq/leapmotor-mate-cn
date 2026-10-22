"""Synthetic replay of CN T03 parking in N, through recording and the Overview."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
import db
import db_reader
import main as web_main
import recorder
from cn_telemetry import decode_status
from state_machine import State, StateMachine
from test_cn_adapter import raw, vehicle
from test_cn_t03 import named, t03
from vehicle_snapshot import SnapshotSignals


@pytest.mark.parametrize('shape', ['data', 'signalMap', 'signal', 'numbered'])
@pytest.mark.parametrize('speed,driving', [(0, False), (1, False), (5, True)])
def test_t03_neutral_keeps_gear_and_uses_speed(shape, speed, driving):
    payload = (raw(**{'1010': 2, '1319': speed}) if shape == 'numbered'
               else named(shape, gearStatus=2, speed=speed, chargeState=0))
    data = decode_status(payload, t03()).data
    assert data.gear == 'N'
    assert data.is_driving is driving
    assert data.vehicle_state == ('driving' if driving else 'parked')


def test_missing_speed_cannot_prove_neutral_is_stationary():
    data = decode_status(named(gearStatus=2, speed=None, chargeState=0), t03()).data
    assert not data.speed_reported and data.is_driving


@pytest.mark.parametrize('model', ['B01', 'B10', 'C10', 'T03'])
@pytest.mark.parametrize('gear', [1, 2, 3])
def test_only_t03_stationary_neutral_changes(model, gear):
    car = dataclasses.replace(vehicle(), car_type=model)
    data = decode_status(raw(**{'1010': gear, '1319': 0}), car).data
    sm = StateMachine()
    sm.update(data)
    assert (sm.state == State.DRIVING) is not (model == 'T03' and gear == 2)


def test_reported_parked_frame_does_not_create_trip_or_fast_poll(tmp_path, monkeypatch):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    database = db.Database(str(tmp_path / 'parking.db'))
    vid = database.ensure_vehicle(t03().vin, 'T03')
    rec = recorder.Recorder(database, vid)
    # Same shape and motion/power values as the owner's report, without private data.
    snapshot = decode_status(named('data', soc=28, totalMileage=49319, expectedMileage=119,
        gearStatus=2, speed=0, chargeState=0, batteryCurrent=0, batteryVoltage=404.9,
        bcmKeyPositionOn3=False, collectTimeMs=None, collectTime='2026-10-22 17:07:32'), t03())
    for _ in range(125):
        rec.process(snapshot.data)  # cloud keeps repeating the same old N frame
        assert rec.state == State.PARKED_ACTIVE and rec.poll_interval == 30
    assert database._conn.execute('SELECT COUNT(*) FROM trips').fetchone()[0] == 0
    assert database._conn.execute('SELECT gear FROM positions LIMIT 1').fetchone()[0] == 'N'
    database._conn.close()


def test_real_poll_cycle_uses_parked_interval_for_t03_neutral(tmp_path, monkeypatch):
    from poll_cycle_fixture import VIN, make_poll
    from test_cn_six_second_polling import PM
    monkeypatch.setenv('MATE_MARKET', 'CN')
    run = make_poll(PM, tmp_path, monkeypatch)
    car = dataclasses.replace(t03(), vin=VIN)
    run.ctx.vehicle = car
    run.db._conn.execute("UPDATE vehicles SET car_type='T03' WHERE id=?", (run.vid,))
    run.db._conn.commit()
    run.db.set_setting('poll_parked', '45')
    payload = named(gearStatus=2, chargeState=0)
    payload['data']['vin'] = VIN
    data = decode_status(payload, car).data
    run(data)
    assert run.ctx.poll_error_count == 0 and run.ctx.interval == 45
    assert run.db.get_open_trip(run.vid) is None
    run.db._conn.close()


def test_web_neutral_rule_is_scoped_to_each_vehicle(tmp_path, monkeypatch):
    path = str(tmp_path / 'two-cars.db')
    database = db.Database(path)
    monkeypatch.setattr(db_reader, 'DB_PATH', path)
    for car in (dataclasses.replace(t03(), vin='SYNTHETIC-T03'), vehicle()):
        vid = database.ensure_vehicle(car.vin, car.car_type)
        snapshot = decode_status(raw(**{'1010': 2, '1319': 0, '1149': 1}), car)
        database.save_position(vid, snapshot.data)
        with db_reader.vehicle_scope(vid):
            status = db_reader.get_latest_status()
        assert web_main._driving(status) is (car.car_type != 'T03')
        assert bool(status['charging']) is (car.car_type == 'T03')
    database._conn.close()


@pytest.mark.parametrize('seconds,confirmations', [(6, 10), (10, 6), (20, 6)])
@pytest.mark.parametrize('ready', [False, None])
def test_t03_neutral_ends_trip_after_confirmation(monkeypatch, seconds, confirmations, ready):
    sm = StateMachine(poll_driving=seconds)
    sm.update(decode_status(named(gearStatus=3, speed=30, chargeState=0), t03()).data)
    assert sm.state == State.DRIVING
    parked = decode_status(named(gearStatus=2, chargeState=0, bcmKeyPositionOn3=ready), t03()).data
    for _ in range(confirmations - 1):
        sm.update(parked)
        assert sm.state == State.DRIVING
    sm.update(parked)
    assert sm.state == State.PARKED_ACTIVE


def test_neutral_ready_on_and_brief_stops_do_not_split_a_drive():
    sm = StateMachine(poll_driving=6)
    drive = decode_status(named(gearStatus=3, chargeState=0), t03()).data
    sm.update(drive)  # D at zero speed still opens a drive
    neutral = decode_status(named(gearStatus=2, chargeState=0, bcmKeyPositionOn3=True), t03()).data
    for _ in range(20):
        sm.update(neutral)
        assert sm.state == State.DRIVING
    neutral.ready = False
    for _ in range(9): sm.update(neutral)
    sm.update(drive)  # departure cancels confirmation
    for _ in range(9): sm.update(neutral)
    assert sm.state == State.DRIVING
    sm.update(neutral)
    assert sm.state == State.PARKED_ACTIVE
    sm.update(drive)
    assert sm.state == State.DRIVING


@pytest.mark.parametrize('code,charging,plugged', [(0, 0, False), (1, 1, True), (2, 0, True),
                                                 (4, 0, True), (6, 0, True)])
def test_neutral_charge_and_web_writers_agree(tmp_path, monkeypatch, code, charging, plugged):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    path = str(tmp_path / 'writers.db')
    database = db.Database(path)
    vid = database.ensure_vehicle(t03().vin, 'T03')
    snapshot = decode_status(named(gearStatus=2, chargeState=code), t03())
    assert snapshot.data.charging_status == charging
    assert snapshot.data.plug_connected is plugged
    sm = StateMachine()
    sm.update(snapshot.data)
    assert sm.state == (State.CHARGING if charging else State.PARKED_ACTIVE)
    database.save_position(vid, snapshot.data)
    monkeypatch.setattr(db_reader, 'DB_PATH', path)
    monkeypatch.setattr(db_reader, '_conn_rw', lambda: database._conn)
    db_reader.save_fresh_signals(SnapshotSignals(snapshot), t03().vin)
    rows = database._conn.execute('SELECT gear, speed_kmh, charging, plug_connected FROM positions').fetchall()
    assert len(rows) == 2 and tuple(rows[0]) == tuple(rows[1])
    with db_reader.vehicle_scope(vid):
        status = db_reader.get_latest_status()
    assert status['gear'] == 'N' and not web_main._driving(status)
    assert status['charging'] == charging and status['plug_connected'] is plugged
    database._conn.close()


def test_restart_does_not_resume_an_old_false_neutral_trip(tmp_path, monkeypatch):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    database = db.Database(str(tmp_path / 'restart.db'))
    vid = database.ensure_vehicle(t03().vin, 'T03')
    data = decode_status(named(gearStatus=2, chargeState=0), t03()).data
    old = dataclasses.replace(data, vehicle_state='driving')
    recorder.Recorder(database, vid).process(old)  # old version's false trip
    assert database.get_open_trip(vid)
    rec = recorder.Recorder(database, vid)
    rec.process(data)
    assert rec.state == State.PARKED_ACTIVE and rec.poll_interval == 30
    assert database.get_open_trip(vid) is None
    database._conn.close()


def test_trip_closes_on_t03_switch_off_in_neutral(tmp_path, monkeypatch):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    now = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
    clock = {'now': now}
    monkeypatch.setattr(db, '_now_iso', lambda: clock['now'].isoformat())
    monkeypatch.setattr(recorder, '_now_iso', lambda: clock['now'].isoformat())
    # CN's serializer uses datetime.now: pin it by replacing the stored times per poll.
    database = db.Database(str(tmp_path / 'trip.db'))
    vid = database.ensure_vehicle(t03().vin, 'T03')
    rec = recorder.Recorder(database, vid)
    original_save = database.save_position
    def save(vid, data):
        pid = original_save(vid, data)
        database._conn.execute('UPDATE positions SET recorded_at=? WHERE id=?', (clock['now'].isoformat(), pid))
        database._conn.commit()
        return pid
    monkeypatch.setattr(database, 'save_position', save)
    def poll(seconds, gear, ready, odo):
        clock['now'] = now + timedelta(seconds=seconds)
        rec.process(decode_status(named(gearStatus=gear, bcmKeyPositionOn3=ready, chargeState=0,
            totalMileage=odo, collectTimeMs=int(clock['now'].timestamp() * 1000)), t03()).data)
    poll(0, 3, True, 1000)
    poll(30, 3, True, 1002)
    poll(60, 2, False, 1002)
    for i in range(1, 10): poll(60 + i * 6, 2, False, 1002)
    assert rec.state == State.PARKED_ACTIVE
    trip = database._conn.execute('SELECT * FROM trips').fetchone()
    assert trip and trip['ended_at'] == (now + timedelta(seconds=60)).isoformat()
    assert trip['distance_km'] == 2
    database._conn.close()
