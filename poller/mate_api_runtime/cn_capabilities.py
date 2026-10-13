"""China hardware and shared-account permissions, independent of EU ability numbers."""
import json
import hashlib
from dataclasses import dataclass

# action: (CN cmd/right, any hardware ability, shared module)
COMMANDS = {
    'lock': (110, (1,), 100), 'unlock': (110, (1,), 100),
    'find_car': (120, (2,), 200), 'open_trunk': (130, (3,), 200),
    'close_trunk': (130, (3,), 200), 'windows': (230, (12, 36, 89), 200),
    'open_windows': (230, (12, 36, 89), 200), 'close_windows': (230, (12, 36, 89), 200),
    'open_sunshade': (240, (13,), 200), 'close_sunshade': (240, (13,), 200),
    'battery_preheat': (160, (10,), 200), 'battery_preheat_off': (160, (10,), 200),
    'climate': (170, (5, 29), 200), 'seat_heat': (301, (21, 22, 85), 200),
    'seat_vent': (370, (42, 43, 67, 93), 200),
    'steering_heat': (320, (32,), 200), 'mirror_heat': (440, (59,), 200),
    'charge_schedule': (190, (14, 15, 20, 34), 200),
    'charge_limit': (190, (14, 15, 20, 34), 200),
    'healthy_charging': (480, (68,), 200),
}
ALIASES = {
    'healthy_charging_on': 'healthy_charging', 'healthy_charging_off': 'healthy_charging',
    'lock_toggle': 'lock', 'trunk': 'open_trunk',
    'door_lock': 'lock', 'door_lock_toggle': 'lock', 'climate_cool': 'climate', 'climate_heat': 'climate',
    'climate_auto': 'climate', 'climate_vent': 'climate', 'fan_level': 'climate',
    'recirculation': 'climate', 'recirc_toggle': 'climate',
    'climate_temperature': 'climate', 'climate_fan': 'climate', 'climate_recirc': 'climate',
    'lock_vehicle': 'lock', 'unlock_vehicle': 'unlock', 'find_vehicle': 'find_car',
    'set_windows': 'windows', 'save_charge_schedule': 'charge_schedule',
    'set_charge_schedule': 'charge_schedule', 'set_charge_limit': 'charge_limit',
    'ac_on': 'climate', 'ac_off': 'climate', 'ac_switch': 'climate',
    'climate_off': 'climate', 'climate_on': 'climate', 'set_climate_temp': 'climate',
    'set_fan_level': 'climate', 'set_recirc': 'climate',
    'quick_cool': 'climate', 'quick_heat': 'climate', 'quick_vent': 'climate',
    'windshield_defrost': 'climate', 'climate_defrost': 'climate',
    'set_seat_heat': 'seat_heat', 'set_seat_vent': 'seat_vent',
    'steering_wheel_heat_on': 'steering_heat', 'steering_wheel_heat_off': 'steering_heat',
    'rearview_mirror_heat_on': 'mirror_heat', 'rearview_mirror_heat_off': 'mirror_heat',
}


def action_key(name):
    if name.startswith('seat_heat_'): return 'seat_heat'
    if name.startswith('seat_vent_'): return 'seat_vent'
    if name.startswith('steering_heat_'): return 'steering_heat'
    if name.startswith('mirror_heat_'): return 'mirror_heat'
    return ALIASES.get(name, name)


def permission(vehicle, action):
    rule = COMMANDS.get(action_key(action))
    if rule is None: return False, '此功能尚未有完整中國區協定，未發送指令'
    right, abilities, module = rule
    if not set(abilities).intersection(vehicle.abilities): return False, '車輛未宣告此功能配備'
    if vehicle.is_shared:
        if right not in vehicle.rights: return False, '共享帳號未獲授權此控制'
        if module not in vehicle.module_rights: return False, '共享帳號未獲授權此功能類別'
    return True, ''


def stored_permission(vin, action, get_setting):
    from types import SimpleNamespace
    try:
        data = json.loads(get_setting('cn_capabilities_' + str(vin).lower(), '') or '{}')
        import crypto
        account = crypto.decrypt(get_setting('leapmotor_user', ''))
        if not account or data.get('account') != hashlib.sha256(account.encode()).hexdigest():
            return False
        return permission(SimpleNamespace(**data), action)[0]
    except (ValueError, TypeError, AttributeError):
        return False
