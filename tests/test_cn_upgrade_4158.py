"""Cold starts keep an existing CN account, per-car history and user preferences."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import mate_api  # noqa: F401 — shared backend import path
import cn_backend
import db
from test_cn_adapter import api, raw, vehicle  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('language,poll', [('zh-TW', '6'), ('zh-CN', '20'), ('en', '10')])
def test_two_process_restart_preserves_the_configured_cn_install(api, language, poll):
    database = db.Database(str(cn_backend.db_path()))
    first = database.ensure_vehicle('SYNTHETIC-T03', 'T03')
    second = database.ensure_vehicle(vehicle().vin, 'B01')
    settings = {'setup_complete': '1', 'language': language, 'currency': 'CNY',
                'timezone': 'Asia/Shanghai', 'timezone_pinned_v1': '1',
                'poll_driving': poll, 'poll_parked': '45', 'cn_poll_driving_6s_migrated': '1',
                'active_vehicle_vin': vehicle().vin, 'battery_capacity_kwh': '36.6'}
    for key, value in settings.items():
        database.set_setting(key, value)
    database.set_secret('leapmotor_pin_synthetic-t03', '1234')
    database.set_secret('leapmotor_pin_' + vehicle().vin.lower(), '5678')
    database._conn.execute('UPDATE vehicles SET capacity_kwh = 36.6 WHERE id = ?', (first,))
    database._conn.execute('UPDATE vehicles SET capacity_kwh = 67.1 WHERE id = ?', (second,))
    from cn_telemetry import decode_status
    database.save_position(second, decode_status(raw(), vehicle()).data)
    database._conn.execute('''INSERT INTO trips(vehicle_id,started_at,ended_at,distance_km,
        start_soc,end_soc,start_odometer_km,end_odometer_km,note)
        VALUES (?, '2026-10-20T01:00:00+00:00', '2026-10-20T01:30:00+00:00',
                10,60,55,90,100,'existing trip')''', (second,))
    database._conn.execute('''INSERT INTO charges(vehicle_id,started_at,ended_at,start_soc,end_soc,
        energy_added_kwh,duration_min,charge_type)
        VALUES (?, '2026-10-20T02:00:00+00:00','2026-10-20T03:00:00+00:00',50,60,6.71,60,'AC')''', (second,))
    database._conn.commit()
    # Finish pre-existing repairs first: this represents an installation that already ran.
    database._conn.close()
    database = db.Database(str(cn_backend.db_path()))
    tables = ('vehicles', 'positions', 'trips', 'charges')
    expected = {table: [dict(row) for row in database._conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                for table in tables}
    session = cn_backend.get_setting(cn_backend.SESSION_KEY)
    database._conn.close()
    check = r'''
import importlib.util, json, sys
sys.path[:0] = sys.argv[1:4]
import no_internet
import mate_api, db_reader, cn_backend
from db import Database
if sys.argv[6] == 'web':
    import main
else:
    from runtime_paths import prepare_installation
    prepare_installation()
    spec = importlib.util.spec_from_file_location('cn_upgrade_poller', sys.argv[2] + '/main.py')
    poller = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = poller
    spec.loader.exec_module(poller)
database = Database(sys.argv[4])
if sys.argv[6] == 'poller':
    poller._migrate_cn_driving_poll(database)
expected, settings, session = json.loads(sys.argv[5])
for table, rows in expected.items():
    assert [dict(row) for row in database._conn.execute('SELECT * FROM ' + table + ' ORDER BY id')] == rows, table
for key, value in settings.items():
    assert db_reader.get_setting(key) == value, key
assert db_reader.get_language() == settings['language']
assert db_reader.get_vehicle()[0]['vin'] == settings['active_vehicle_vin']
assert db_reader.get_operate_pin('SYNTHETIC-T03') == '1234'
assert db_reader.get_operate_pin('SYNTHETIC-B01') == '5678'
def no_cloud(*args, **kwargs):
    raise AssertionError('restart attempted a cloud request')
client = cn_backend.ChinaAPIClient(username='13800000000', transport=no_cloud)
client.login()
assert cn_backend.get_setting(cn_backend.SESSION_KEY) == session
assert database._conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
database._conn.close()
print('existing CN installation retained')
'''
    args = [sys.executable, '-c', check, str(ROOT / 'web'), str(ROOT / 'poller'),
            str(ROOT / 'tests'), str(cn_backend.db_path()), json.dumps([expected, settings, session])]
    # The restart also reuses the first startup's backup; both paths must remain usable.
    for role in ('web', 'poller', 'web'):
        result = subprocess.run(args + [role], env=dict(os.environ), capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'existing CN installation retained' in result.stdout
