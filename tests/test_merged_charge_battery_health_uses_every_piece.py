"""Battery-health telemetry must span every piece of a merged charge session."""
from datetime import datetime, timedelta, timezone

import pytest

import db as D
import db_reader


def test_a_merged_charge_health_uses_energy_from_every_piece(tmp_path, monkeypatch):
    path = str(tmp_path / "merged-soh.db")
    pdb = D.Database(path)
    c = pdb._conn
    c.execute("INSERT INTO vehicles (id, vin, car_type, capacity_kwh) VALUES (1,'SOHTEST','C10',60.0)")
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('battery_capacity_nominal_kwh','60')")

    start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    split = start + timedelta(minutes=30)
    merged_end = start + timedelta(minutes=120)
    separate_start = start + timedelta(minutes=90)
    separate_end = separate_start + timedelta(minutes=60)
    c.execute(
        "INSERT INTO charges (id,vehicle_id,started_at,ended_at,start_soc,end_soc,charge_type) "
        "VALUES (1,1,?,?,20,25,'AC')", (start.isoformat(), split.isoformat()))
    c.execute(
        "INSERT INTO charges (id,vehicle_id,started_at,ended_at,start_soc,end_soc,charge_type,merged_into_id) "
        "VALUES (2,1,?,?,25,40,'AC',1)", (split.isoformat(), merged_end.isoformat()))
    c.execute(
        "INSERT INTO charges (id,vehicle_id,started_at,ended_at,start_soc,end_soc,charge_type) "
        "VALUES (3,1,?,?,35,50,'AC')", (separate_start.isoformat(), separate_end.isoformat()))

    # The merged session stores 12 kWh over 20 SoC points at 6 kW. A later
    # independent session starts before the merged child’s stale ended_at and charges
    # at 12 kW; neither its energy nor its SoC may leak into the merged estimate.
    for minute in range(0, 186, 5):
        t = start + timedelta(minutes=minute)
        if minute <= 30:
            soc = 20 + 5 * minute / 30
        elif minute <= 90:
            soc = 25 + 15 * (minute - 30) / 90
        else:
            soc = 35 + 15 * (minute - 90) / 60
        amps = 25 if minute < 90 else 50
        c.execute(
            "INSERT INTO positions (vehicle_id,recorded_at,soc,charging,charge_voltage_v,"
            "charge_current_a,battery_min_temp) VALUES (1,?,?,1,240,?,20)",
            (t.isoformat(), soc, amps))
    c.commit()
    c.close()

    monkeypatch.setattr(db_reader, "DB_PATH", path)
    monkeypatch.setattr(db_reader, "_current_vehicle_id", lambda: 1)
    bounds = db_reader._power_window_bounds(
        db_reader._get(), start.isoformat(), merged_end.isoformat(), (1, 2))
    assert bounds[1:] == (db_reader._iso_to_utc(separate_start.isoformat()), True)
    health = db_reader.get_battery_health()

    assert health["sample_count"] == 1, health["points"]
    point = health["points"][0]
    assert point["charge_id"] == 1
    assert point["soc_delta_used"] == pytest.approx(14.2, abs=0.2)
    assert point["energy_kwh"] == pytest.approx(8.5, abs=0.1)
    assert point["capacity_kwh"] == pytest.approx(60.0, abs=0.5)
    assert health["latest_soh_pct"] == pytest.approx(100.0, abs=1.0)
