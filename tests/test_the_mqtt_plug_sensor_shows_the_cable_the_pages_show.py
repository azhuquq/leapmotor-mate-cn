"""The MQTT plug sensor says what the pages say: the cable is in when the charge session says so, or when
the AC port reports a cable on a parked car (#429).

Since 4.10.0 the pages read the cable from the session (signal 1149) or from the AC port while parked
(signal 47 at 1), because a car that has finished its charge, or waits for its schedule, ends the session
with the cable still in. MQTT kept publishing the session alone: a C10 with the cable in after its charge
showed "Plugged" on the Overview and in the Leapmotor app, and OFF in Home Assistant. The session itself,
which the poller uses to open and close charges, is untouched.
"""
import dataclasses
import datetime as dt
import itertools

import pytest

M = pytest.importorskip("mqtt", reason="poller.mqtt needs paho")

import db as D
import db_reader
from client import VehicleData

VIN = "VINTEST"


class _FakeClient:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload


def _data(**kw):
    """A real VehicleData, parked, cable out and switched off, apart from what the test sets."""
    base = VehicleData(
        vin=VIN, timestamp_ms=0, soc=50, range_km=200, odometer_km=1000.0, speed_kmh=0,
        gear="P", vehicle_state="parked", charging_status=0, charge_power_kw=0.0,
        latitude=45.0, longitude=9.0, outside_temp=20, inside_temp=22, climate_target_temp=22,
        battery_min_temp=20, is_locked=True, climate_on=False, climate_cooling=False,
        climate_heating=False, climate_defrost=False, trunk_open=False, windows_open=False,
        sunshade_pct=0, any_door_open=False, plug_connected=False,
        remaining_charge_min=0, charge_voltage_v=0.0, charge_current_a=0.0,
    )
    return dataclasses.replace(base, **kw)


def _published(data):
    svc = M.MqttService("broker", 1883, topic_prefix="leapmotor", get_setting=lambda k, d="": d)
    svc.client = _FakeClient()
    svc._publish_sensors(data)
    p = svc.client.published
    return p[f"leapmotor/{VIN}/plug_connected"], p[f"leapmotor/{VIN}/evcc/plugged"]


def test_the_cable_the_port_reports_after_the_session_ended_is_on():
    assert _published(_data(plug_connected=False, ac_port_mode=1)) == ("ON", "true")


def test_a_charge_session_is_on_as_before():
    assert _published(_data(plug_connected=True, ac_port_mode=1, charging_status=1)) == ("ON", "true")


def test_no_cable_is_off():
    assert _published(_data(plug_connected=False, ac_port_mode=0)) == ("OFF", "false")


def test_a_moving_car_has_no_cable_in_its_port():
    assert _published(_data(plug_connected=False, ac_port_mode=1, gear="D", speed_kmh=40)) == ("OFF", "false")


def test_the_v2l_adapter_is_not_a_charging_cable():
    assert _published(_data(plug_connected=False, ac_port_mode=2)) == ("OFF", "false")


def test_mqtt_and_the_pages_agree_on_every_reading(tmp_path, monkeypatch):
    """The two copies of the rule disagreed from 4.10.0 to 4.15.5 without a test noticing: this one reads
    the same frames through both and asks for one answer."""
    path = str(tmp_path / "t.db")
    c = D.Database(path)._conn
    c.execute("INSERT INTO vehicles (id, vin, car_type) VALUES (1,?,'C10')", (VIN,))
    c.commit()
    monkeypatch.setattr(db_reader, "DB_PATH", path)
    monkeypatch.setattr(db_reader, "_current_vehicle_id", lambda: 1)
    now = dt.datetime.now(dt.timezone.utc)
    combos = itertools.product((False, True), (0, 1, 2), (("P", 0), ("P", 3), ("D", 0), ("D", 40)))
    for k, (plug, port, (gear, speed)) in enumerate(combos):
        c.execute("INSERT INTO positions (vehicle_id, recorded_at, soc, gear, speed_kmh, charging,"
                  " plug_connected, ac_port_mode) VALUES (1, ?, 50, ?, ?, 0, ?, ?)",
                  ((now + dt.timedelta(seconds=k)).isoformat(), gear, speed, int(plug), port))
        c.commit()
        page = db_reader.get_latest_status()["cable_connected"]
        sensor, _ = _published(_data(plug_connected=plug, ac_port_mode=port, gear=gear, speed_kmh=speed))
        assert (sensor == "ON") is page, (plug, port, gear, speed)
    c.close()
