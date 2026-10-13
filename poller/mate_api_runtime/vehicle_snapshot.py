"""Shared normalized telemetry; raw cloud IDs never serve as application state."""
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class VehicleData:
    vin: str
    timestamp_ms: int
    soc: float
    range_km: Optional[float]           # None when the car reports none (#365)
    odometer_km: float
    speed_kmh: float
    gear: str            # P R N D
    vehicle_state: str   # parked driving
    charging_status: int
    charge_power_kw: float | None    # |I×V| from 1178/1177; None = no reading the car can vouch for
    latitude: float
    longitude: float
    outside_temp: float
    inside_temp: float
    climate_target_temp: float
    battery_min_temp: float
    is_locked: bool
    climate_on: bool
    climate_cooling: bool     # quick-cool active (signal 2669 == 2)
    climate_heating: bool     # quick-heat active (signal 2681 == 2)
    climate_defrost: bool     # windshield defrost active (signal 1945 == 2)
    trunk_open: bool
    windows_open: bool
    sunshade_pct: int | None  # how far the sunshade is open, % (signal 1724); 0 = closed, None = not said
    any_door_open: bool       # driver/passenger/rear doors or trunk
    plug_connected: bool      # cable inserted (signal 1149)
    remaining_charge_min: int # minutes to full (signal 1200), 0 when not charging
    charge_voltage_v: float | None   # pack voltage (signal 1177); None = the car did not say
    charge_current_a: float | None   # pack current (signal 1178), + discharge / − charge; None = not said
    speed_reported: bool = True     # whether the frame carried 1319; if not: speed_kmh 0, `positions` NULL
    odometer_reported: bool = True  # whether the frame carried 1318; if not: odometer_km 0, `positions` NULL
    is_reev: bool = False     # car reports a fuel tank (signal 3235) → range-extender model
    fuel_level_pct: float = None  # REEV fuel tank level % (signal 3235); None on a BEV
    # Litres actually in the tank — signal 3263, reported in MILLILITRES. Decoded by @gm27271
    # (beta #10) and confirmed here across seven bundles from three owners: 3263 ÷ 3235 is constant
    # to ±0.05 L within a model, and the highest value ever seen on a full C10 tank is exactly
    # 47 500. It makes the tank-size constant unnecessary wherever it is present — the car counts
    # the litres itself, finely enough to argue with a pump (his fill: 34.416 L where the pump said
    # 33.84). None on a BEV.
    fuel_liters: float = None
    fuel_range_km: float = None       # REEV range on fuel alone (signal 3259); None on a BEV
    combined_range_km: float = None   # REEV total range = battery + fuel (signal 3261); None on a BEV
    raw_signals: dict = None  # full raw signal dict — attached in research/beta mode for full-PID logging
    # Individual doors / windows / tyres — used by the optional MQTT → HA bridge
    door_driver_open: bool = False
    door_passenger_open: bool = False
    door_rear_left_open: bool = False
    door_rear_right_open: bool = False
    window_fl_open: bool = False
    window_fr_open: bool = False
    window_rl_open: bool = False
    window_rr_open: bool = False
    tire_fl_bar: float = 0.0
    tire_fr_bar: float = 0.0
    tire_rl_bar: float = 0.0
    tire_rr_bar: float = 0.0
    # Comfort STATE sensors (read-only). They reflect reality on the B10 even though the
    # matching remote COMMANDS don't actuate (see capability_profile). 0 = off, >0 = level/on.
    seat_heat_driver: int = 0       # signal 2100 driverSeatHeating
    seat_heat_passenger: int = 0    # signal 2118 passengerSeatHeating
    seat_vent_driver: int = 0       # signal 2101 driverSeatVentilation
    seat_vent_passenger: int = 0    # signal 2119 passengerSeatVentilation
    steering_heat: int = 0          # signal 1816 steeringWheelHeating
    mirror_heat_left: int = 0       # signal 49 leftMirrorHeating
    mirror_heat_right: int = 0      # signal 50 rightMirrorHeating
    ready: bool = False             # signal 1258 bcmKeyPositionOn3 — faithful READY/ON3 (physical key only)
    ready_reported: bool = True     # whether the frame carried 1258; if not: ready False, `positions` NULL
    charge_completed: bool = False  # signal 3736 chargeCompleted — not a full-charge flag, see _parse_signal
    security_active: bool | None = None   # signal 1255 vehicleSecurityActive — None = the car never said
    charge_limit_percent: int | None = None  # configured max-charge SoC (from the config block, not a
                                             # signal); None if unknown / model doesn't report it
    # AC-port / V2L mode — signal 47 (acInputSlowCharge): 0 = AC port idle, 1 = AC charging (input;
    # LATCHES at 1 for ~5-10 min after a charge, see _is_plugged_in), 2 = V2L bidirectional discharge
    # active. Verified on-car 2026-06-19: V2L switch ON + adapter → 47=2, battery discharging (1178>0).
    ac_port_mode: int = 0
    dc_gun_connected: bool | None = None  # DC fast-charge gun inserted (signal 1197); None = not reported

    # Climate detail (read+write validated on-car 2026-06-20): fan level (signal 1941 acAirVolume,
    # 1-7; HOLDS the last level even when A/C is off), recirculation (signal 1943: 1=recirc/in,
    # 0=fresh/out), base mode (signal 3713: 0=auto·1=cool·3=heat·4=vent). NB: the old SDK
    # mislabels 1941 as drive_status — the on-car diff proved it's the fan air-volume.
    fan_level: int = 0
    recirculation: bool = False
    climate_mode: int | None = None
    # Cabin PTC power in watts (signal 1348, 50 W steps 0..2700). Measured across the beta
    # channel; NOT the range-extender generator (disproved by @michapr) nor traction.
    climate_power: int | None = None

    # Cable inserted AND the charge deliberately postponed to the programmed window (1149 == 4).
    # Kept apart from plug_connected because the two answer different questions: the Overview and
    # the MQTT sensor want "is the cable in", the session machinery wants "is a charge running".
    charge_deferred: bool = False
    market: str = "EU"
    charge_state_code: int | None = None
    reported_fields: frozenset[str] | None = None
    collect_time_ms: float | None = None
    windows_open_count: int | None = None

    @property
    def v2l_active(self) -> bool:
        """True when the AC port is in V2L / bidirectional-discharge mode (powering an external
        load). The DELIVERED power is gated separately on real discharge current (1178)."""
        return self.ac_port_mode == 2

    @property
    def climate_mode_label(self) -> str:
        """Human label for the base climate mode (signal 3713); '' if unknown / no data."""
        return {0: "auto", 1: "cool", 3: "heat", 4: "vent"}.get(self.climate_mode, "")

    def fingerprint(self) -> tuple:
        """Compact snapshot of signals that indicate car activity."""
        return (
            self.is_locked,
            round(self.soc),           # 1% granularity avoids noise
            # ⚠️ None-tolerant since #144: a car that never sends the cabin temperature now stores
            # None rather than a fabricated 0.0, and `round(None)` raised — from inside the activity
            # fingerprint, which every poll computes. The tests caught it; a T03 would have caught
            # it in production, by not polling at all.
            None if self.inside_temp is None else round(self.inside_temp),   # 1°C granularity
            self.any_door_open,
            self.charging_status,
            self.plug_connected,
            self.ac_port_mode,         # V2L/charge engaging is activity → triggers the fast (alert) cadence
        )


@dataclass
class VehicleSnapshot:
    data: VehicleData
    raw_signals: dict
    reported_fields: frozenset[str]
    detail: dict

    def status(self):
        out = asdict(self.data)
        out.pop("raw_signals", None)
        out.pop("reported_fields", None)
        out.update(self.detail)
        out["charging"] = self.data.charging_status if 'charging_status' in self.reported_fields else None
        out["frame_ts"] = self.data.timestamp_ms or None
        out["windows_open_count"] = self.detail.get("windows_open_count", self.data.windows_open_count)
        out["charge_completed"] = False
        for key in tuple(out):
            if key in TELEMETRY_FIELDS and key not in self.reported_fields:
                out[key] = None
        return out

    def panel(self):
        status = self.status()
        return {
            'tyres': {corner: {'bar': status.get('tire_' + corner + '_bar'), 'low': None} for corner in ('fl', 'fr', 'rl', 'rr')},
            'doors': {key: status.get(field) for key, field in {'driver': 'door_driver_open', 'passenger': 'door_passenger_open', 'rear_left': 'door_rear_left_open', 'rear_right': 'door_rear_right_open', 'trunk': 'trunk_open'}.items()},
            'windows': {**{key: None for key in ('fl', 'fr', 'rl', 'rr', 'fl_pct', 'fr_pct', 'rl_pct', 'rr_pct')},
                        'sunshade': None if status.get('sunshade_pct') is None else bool(status['sunshade_pct']),
                        'sunshade_pct': status.get('sunshade_pct'), 'any_open': status.get('windows_open')},
            'temps': {'battery': status.get('battery_min_temp'), 'cabin': status.get('inside_temp')},
            'climate': {'on': status.get('climate_on'), 'mode': self.data.climate_mode_label or None,
                        'fan': status.get('fan_level'), 'recirc': status.get('recirculation'), 'target': status.get('climate_target_temp')},
        }


TELEMETRY_FIELDS = frozenset(f.name for f in __import__("dataclasses").fields(VehicleData)) - {"vin", "market", "raw_signals", "reported_fields", "timestamp_ms", "vehicle_state", "is_reev"}


class SnapshotSignals(dict):
    """Compatibility container holding untouched raw IDs plus the shared snapshot."""
    def __init__(self, snapshot):
        super().__init__(snapshot.raw_signals)
        self.snapshot = snapshot
