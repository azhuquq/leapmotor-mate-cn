"""Decode CN semantics once for the poller, web, recorder and MQTT."""
import dataclasses
import math
from leapmotor_cn.protocol import gcj02_to_wgs84
from vehicle_snapshot import VehicleData, VehicleSnapshot


def number(value):
    if value is None or isinstance(value, bool): return None
    try: result = float(value)
    except (TypeError, ValueError): return None
    return result if math.isfinite(result) else None


def decode_status(payload, vehicle):
    from cn_backend import ChinaError
    body = payload.get('data') or {}
    if body.get('vin') and body['vin'].lower() != vehicle.vin.lower(): raise ChinaError('車況回應車輛不一致')
    raw = body.get('signalMap')
    if not isinstance(raw, dict) or not raw: raise ChinaError('車輛暫未提供有效車況')
    raw = {str(k): v for k, v in raw.items()}
    values = {field.name: (field.default if field.default is not dataclasses.MISSING else None)
              for field in dataclasses.fields(VehicleData)}
    reported = set()
    def read(field, *ids, kind=float, default=None):
        found = next((number(raw.get(sid)) for sid in ids if number(raw.get(sid)) is not None), None)
        if found is not None:
            values[field] = kind(found); reported.add(field)
        else: values[field] = default
        return values[field]
    soc = read('soc', '100003', '1204')
    if soc is None or not 0 <= soc <= 100: raise ChinaError('車況沒有有效電量，未保存此幀')
    read('range_km', '2188', '3257', '3260')
    read('odometer_km', '1318', default=0.0)
    read('speed_kmh', '1319', default=0.0)
    values['speed_reported'] = 'speed_kmh' in reported
    values['odometer_reported'] = 'odometer_km' in reported
    gear = number(raw.get('1010'))
    values['gear'] = {0: 'P', 1: 'R', 2: 'N', 3: 'D'}.get(gear, 'P')
    if gear in (0, 1, 2, 3): reported.add('gear')
    driving = values['gear'] in ('R', 'N', 'D') or values['speed_kmh'] > 1
    values['vehicle_state'] = 'driving' if driving else 'parked'
    timestamp = number(raw.get('sts')) or number(raw.get('1'))
    if timestamp and 1_000_000_000 <= timestamp < 100_000_000_000: timestamp *= 1000
    values.update(vin=vehicle.vin, timestamp_ms=int(timestamp or 0), market='CN', raw_signals=raw)
    charge = number(raw.get('1149'))
    code = int(charge) if charge is not None and charge.is_integer() else None
    values['charge_state_code'] = code
    if code is not None: reported.add('charge_state_code')
    known = code in (0, 1, 2, 3, 4, 5, 6)
    values['charging_status'] = int(code == 1 and not driving)
    values['plug_connected'] = code in (1, 2, 3, 4, 6) and not driving
    values['charge_deferred'] = code == 4 and not driving
    if known: reported.update(('charging_status', 'plug_connected', 'charge_deferred'))
    read('charge_voltage_v', '1177')
    read('charge_current_a', '1178')
    amps, volts = values['charge_current_a'], values['charge_voltage_v']
    values['charge_power_kw'] = abs(amps * volts) / 1000 if amps is not None and volts is not None else None
    if values['charge_power_kw'] is not None: reported.add('charge_power_kw')
    read('remaining_charge_min', '1200', kind=int, default=0)
    read('inside_temp', '1349')
    read('climate_target_temp', '2183')
    read('battery_min_temp', '1182')
    values['outside_temp'] = None
    for name, sid in {'is_locked': '1298', 'climate_on': '1938', 'ready': '1258'}.items():
        read(name, sid, kind=lambda n: n == 1, default=False)
    values['ready_reported'] = 'ready' in reported
    for name, sid in {'climate_cooling': '2669', 'climate_heating': '2681', 'climate_defrost': '1945'}.items():
        read(name, sid, kind=lambda n: n == 2, default=False)
    for name, sid in {'trunk_open': '1281', 'door_driver_open': '1277', 'door_passenger_open': '1278',
                      'door_rear_left_open': '1279', 'door_rear_right_open': '1280', 'dc_gun_connected': '1197'}.items():
        read(name, sid, kind=lambda n: n != 0, default=False if name != 'dc_gun_connected' else None)
    values['any_door_open'] = any(values[k] for k in ('trunk_open', 'door_driver_open', 'door_passenger_open', 'door_rear_left_open', 'door_rear_right_open'))
    window_values = [number(raw.get(k)) for k in ('1693', '1694', '1695', '1696')]
    values['windows_open'] = any(v is not None and v != 0 for v in window_values)
    if any(v is not None for v in window_values): reported.add('windows_open')
    # Per-corner ordering is not established in the CN corpus. Never fabricate four corner states.
    for name in ('window_fl_open', 'window_fr_open', 'window_rl_open', 'window_rr_open'): values[name] = False
    read('sunshade_pct', '1724', kind=int)
    for name, sid in {'fan_level': '1941', 'climate_mode': '3713', 'climate_power': '1348',
                      'ac_port_mode': '47', 'seat_heat_driver': '2100', 'seat_heat_passenger': '2118',
                      'seat_vent_driver': '2101', 'seat_vent_passenger': '2119', 'steering_heat': '1816',
                      'mirror_heat_left': '49', 'mirror_heat_right': '50'}.items():
        read(name, sid, kind=int, default=None if name in ('climate_mode', 'climate_power') else 0)
    read('recirculation', '1943', kind=lambda n: n == 1, default=False)
    # 1255 is not validated as a CN security state; leave it unknown.
    values['security_active'] = None
    values['charge_completed'] = False
    values['charge_limit_percent'] = None
    for corner, sid in {'fl': '2646', 'fr': '2653', 'rl': '2660', 'rr': '2667'}.items():
        read('tire_' + corner + '_bar', sid, kind=lambda n: n / 100, default=0.0)
    read('fuel_level_pct', '3235')
    read('fuel_liters', '3263', kind=lambda n: n / 1000)
    read('fuel_range_km', '3259')
    read('combined_range_km', '3261', '3258')
    # BEVs can carry zero-filled fuel fields. Positive fuel evidence is required.
    values['is_reev'] = any((values[k] or 0) > 0 for k in ('fuel_level_pct', 'fuel_liters', 'fuel_range_km'))
    if not values['is_reev']:
        for k in ('fuel_level_pct', 'fuel_liters', 'fuel_range_km', 'combined_range_km'):
            values[k] = None; reported.discard(k)
    lat = next((number(raw.get(k)) for k in ('3', '3725', '2190') if number(raw.get(k)) is not None), None)
    lon = next((number(raw.get(k)) for k in ('2', '3724', '2191') if number(raw.get(k)) is not None), None)
    permitted_gps = not vehicle.is_shared or 300 in vehicle.module_rights
    if permitted_gps and lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
        values['latitude'], values['longitude'] = gcj02_to_wgs84(lat, lon)
        reported.update(('latitude', 'longitude'))
    else: values['latitude'] = values['longitude'] = 0.0
    values['reported_fields'] = frozenset(reported)
    data = VehicleData(**values)
    detail = {'collect_time_ms': number(body.get('collectTime')),
              'charge_phase': {0: 'unplugged', 1: 'charging', 2: 'stopped', 3: 'fault', 4: 'deferred', 5: 'unplugged', 6: 'paused'}.get(code, 'unknown'),
              'windows_open_count': sum(v != 0 for v in window_values if v is not None) if all(v is not None for v in window_values) else None,
              'climate_venting': values['climate_on'] and values['climate_mode'] == 4}
    data.collect_time_ms = detail['collect_time_ms']
    data.windows_open_count = detail['windows_open_count']
    data.reported_fields = frozenset(reported | {'collect_time_ms', 'windows_open_count'})
    return VehicleSnapshot(data, raw, data.reported_fields, detail)


def save_position(connection, vehicle_id, snapshot):
    """One named-field serializer used by both writers, preserving omitted readings as NULL."""
    from datetime import datetime, timezone
    values = snapshot.status()
    values.update(vehicle_id=vehicle_id, recorded_at=datetime.now(timezone.utc).isoformat(),
                  cn_charge_state=snapshot.data.charge_state_code, market='CN')
    values['ready'] = snapshot.data.ready if snapshot.data.ready_reported else None
    values['charging'] = snapshot.data.charging_status if 'charging_status' in snapshot.reported_fields else None
    columns = {row[1] for row in connection.execute('PRAGMA table_info(positions)')}
    row = {k: v for k, v in values.items() if k in columns and k != 'id'}
    fields = ','.join(row)
    placeholders = ','.join('?' for _ in row)
    cursor = connection.execute(f'INSERT INTO positions ({fields}) VALUES ({placeholders})', tuple(row.values()))
    connection.commit()
    return cursor.lastrowid
