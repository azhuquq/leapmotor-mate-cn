"""EVCC MQTT mirror topics: plug/charging/climate must publish true/false (not ON/OFF),
since EVCC's Go config parser (strconv.ParseBool) rejects ON/OFF. See poller/mqtt.py
_publish_evcc and docs/EVCC.md."""
import types

import pytest

# poller/mqtt.py imports paho at module top; skip cleanly where paho isn't installed
# (the CI test env per pytest.ini), like the other paho-dependent paths.
mqtt_mod = pytest.importorskip("mqtt", reason="poller.mqtt needs paho")


class _FakeClient:
    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload, retain=False):
        self.pubs.append((topic, payload))


def _svc():
    svc = mqtt_mod.MqttService(broker="x", port=1883)
    svc.client = _FakeClient()
    return svc


def _data(**kw):
    base = dict(plug_connected=None, charging_status=0, climate_on=None)
    base.update(kw)
    base["cable_connected"] = base["plug_connected"]   # the cable as the pages read it (#429)
    return types.SimpleNamespace(**base)


def test_evcc_true_when_on():
    svc = _svc()
    svc._publish_evcc("leapmotor/VIN", _data(plug_connected=True, charging_status=2, climate_on=True))
    d = dict(svc.client.pubs)
    assert d["leapmotor/VIN/evcc/plugged"] == "true"
    assert d["leapmotor/VIN/evcc/charging"] == "true"
    assert d["leapmotor/VIN/evcc/climate"] == "true"


def test_evcc_false_when_off():
    svc = _svc()
    svc._publish_evcc("leapmotor/VIN", _data(plug_connected=False, charging_status=0, climate_on=False))
    d = dict(svc.client.pubs)
    assert d["leapmotor/VIN/evcc/plugged"] == "false"
    assert d["leapmotor/VIN/evcc/charging"] == "false"
    assert d["leapmotor/VIN/evcc/climate"] == "false"


def test_evcc_blank_when_unknown():
    # None (unknown) → empty payload, never the string "None"
    svc = _svc()
    svc._publish_evcc("leapmotor/VIN", _data(plug_connected=None, charging_status=None, climate_on=None))
    d = dict(svc.client.pubs)
    assert d["leapmotor/VIN/evcc/plugged"] == ""
    assert d["leapmotor/VIN/evcc/charging"] == "false"  # (None or 0) > 0 → false
    assert d["leapmotor/VIN/evcc/climate"] == ""
