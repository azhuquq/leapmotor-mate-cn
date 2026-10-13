"""A parked car whose GPS goes quiet keeps its outside temperature (13/10/2026).

From 03:01 on 13 October the cloud sent all six GPS signals of a B10 as zero (2, 3, 2190, 2191,
3724, 3725), for hours, while everything else in the frame was normal and the odometer stood still
at 6018 km. The sampler treats 0,0 as no fix and keeps its last reading — but that reading lives
only in memory: after a restart it is None, and since every later position carries no outside
temperature, the Overview hides the row once `never_reported_temps` has seen enough polls.

The car had not moved, so its last real fix is still where it is. With no fix, the sampler now asks
for the weather there — but only when the odometer says the car is where that fix was taken. A car
that has driven since, or a frame with no odometer, is still not guessed at.

The last fix comes from memory while the poller runs, and from the database after a restart.
"""
import pathlib

import db as D
import outside_temp as OT
import poll_cycle_fixture as F


class _Spy:
    """Stands in for the Open-Meteo call and remembers where it was asked about."""
    def __init__(self, answer=12.0):
        self.calls, self.answer = [], answer

    def __call__(self, lat, lon):
        self.calls.append((lat, lon))
        return self.answer


# ── the sampler ──────────────────────────────────────────────────────────────

def test_a_car_that_has_not_moved_is_asked_about_where_it_last_was():
    spy = _Spy(12.0)
    s = OT.OutsideTempSampler(fetch=spy)
    assert s.sample(45.4, 9.2, 1_000.0, 6018.0) == 12.0

    spy.answer = 15.0
    # an hour later, no fix, same odometer: the reading is stale, so ask again — where it parked
    assert s.sample(0.0, 0.0, 1_000.0 + 3600, 6018.0) == 15.0
    assert spy.calls == [(45.4, 9.2), (45.4, 9.2)]


def test_a_car_that_has_driven_since_is_not_guessed_at():
    spy = _Spy(12.0)
    s = OT.OutsideTempSampler(fetch=spy)
    assert s.sample(45.4, 9.2, 1_000.0, 6018.0) == 12.0

    spy.answer = -99.0
    assert s.sample(0.0, 0.0, 1_000.0 + 3600, 6031.0) == 12.0   # 13 km on: somewhere else
    assert spy.calls == [(45.4, 9.2)]


def test_a_frame_with_no_odometer_is_not_guessed_at():
    spy = _Spy(12.0)
    s = OT.OutsideTempSampler(fetch=spy)
    assert s.sample(45.4, 9.2, 1_000.0, 6018.0) == 12.0

    spy.answer = -99.0
    assert s.sample(0.0, 0.0, 1_000.0 + 3600, None) == 12.0
    assert spy.calls == [(45.4, 9.2)]


def test_after_a_restart_the_last_fix_comes_from_what_was_stored():
    spy = _Spy(15.0)
    s = OT.OutsideTempSampler(fetch=spy, last_fix=(45.4, 9.2, 6018.0))
    assert s.sample(0.0, 0.0, 1_000.0, 6018.0) == 15.0
    assert spy.calls == [(45.4, 9.2)]


def test_a_stored_fix_without_an_odometer_is_not_used():
    spy = _Spy(15.0)
    s = OT.OutsideTempSampler(fetch=spy, last_fix=(45.4, 9.2, None))
    assert s.sample(0.0, 0.0, 1_000.0, 6018.0) is None
    assert spy.calls == []


# ── the database ─────────────────────────────────────────────────────────────

def _position(d, vid, lat, lon, odo):
    d._conn.execute("INSERT INTO positions (vehicle_id, recorded_at, latitude, longitude, odometer_km)"
                    " VALUES (?, '2026-10-13T01:00:00+00:00', ?, ?, ?)", (vid, lat, lon, odo))
    d._conn.commit()


def test_the_last_fix_skips_the_frames_without_one(tmp_path):
    d = D.Database(str(tmp_path / "fix.db"))
    vid = d.ensure_vehicle("VINFIX", "B10")
    other = d.ensure_vehicle("VINOTHER", "C10")
    _position(d, vid, 45.0, 9.0, 6000.0)
    _position(d, vid, 45.4, 9.2, 6018.0)
    _position(d, vid, 0.0, 0.0, 6018.0)
    _position(d, other, 41.9, 12.5, 100.0)      # another car's fix is not this one's
    assert d.last_fix(vid) == (45.4, 9.2, 6018.0)


def test_a_car_never_located_has_no_last_fix(tmp_path):
    d = D.Database(str(tmp_path / "nofix.db"))
    vid = d.ensure_vehicle("VINFIX", "B10")
    _position(d, vid, 0.0, 0.0, 6018.0)
    assert d.last_fix(vid) is None


# ── one poll, after a restart ────────────────────────────────────────────────

def test_the_first_poll_after_a_restart_stores_the_weather_where_the_car_parked(tmp_path, monkeypatch):
    """The 13/10 case end to end: the last real fix is on disk, the poller has just started, the
    frame has no GPS and the same odometer — and the position it stores carries a temperature."""
    PM = F.poller_main("poller_main_parked_weather")
    spy = _Spy(15.0)
    monkeypatch.setattr(PM, "OutsideTempSampler",
                        lambda **kw: OT.OutsideTempSampler(fetch=spy, **kw))
    for name in ("_maybe_refresh_charge_schedule", "_write_comfort_state", "_mqtt_tick"):
        monkeypatch.setattr(PM, name, lambda *a, **k: None)
    monkeypatch.setattr(PM.energy_snapshots, "maybe_sample", lambda *a, **k: None)
    monkeypatch.setattr(PM.ready_automation, "maybe_trigger", lambda *a, **k: None)

    db = D.Database(str(tmp_path / "restart.db"))
    vid = db.ensure_vehicle(F.VIN, "B10")
    db.set_setting("outside_temp_enabled", "1")
    _position(db, vid, 45.4, 9.2, 1000.0)       # before the restart: a real fix at 1000 km

    class _Vehicle:
        vin, car_type, year, abilities, is_shared = F.VIN, "B10", 2025, None, False

    ctx = PM.VehicleContext(db, _Vehicle(), vid)
    client = F.Client()
    client.next = F.frame(1_760_000_000_000)
    client.next.latitude = client.next.longitude = 0.0   # no GPS; the frame's odometer is 1000 km
    PM._poll_vehicle(db, client, ctx, PM.AccountState())

    assert spy.calls == [(45.4, 9.2)]
    row = db._conn.execute("SELECT outside_temp FROM positions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["outside_temp"] == 15.0
