"""CN uses 6s on the real polling path, without shortening trip-end confirmation."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from poll_cycle_fixture import NOW, VIN, frame, make_poll, poller_main
from state_machine import State, StateMachine

PM = poller_main('cn_six_second_poller')


@pytest.mark.parametrize('state', [State.DRIVING, State.PARKED_ALERT])
def test_cn_fast_states_default_to_six_seconds(monkeypatch, state):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    sm = StateMachine(state=state)
    assert sm.poll_interval == 6
    sm.state = State.PARKED_ACTIVE
    assert sm.poll_interval == 30
    sm.state = State.CHARGING
    assert sm.poll_interval == 30
    sm._v2l_active = True
    assert sm.poll_interval == 6


@pytest.mark.parametrize('stored,boost,expected', [(None, False, 6), ('6', False, 6),
    ('2', False, 6), ('20', False, 20), (None, True, 6)])
def test_actual_poll_cycle_uses_new_default_clamp_and_boost(tmp_path, monkeypatch, stored, boost, expected):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    run = make_poll(PM, tmp_path, monkeypatch)
    if stored is not None:
        run.db.set_setting('poll_driving', stored)
    if boost:
        run.db.set_boost(VIN, NOW + 60)
    data = frame(int(NOW * 1000))
    data.gear, data.speed_kmh = ('P', 0) if boost else ('D', 30)
    run(data)
    assert run.ctx.poll_error_count == 0
    assert run.ctx.interval == expected
    assert run.db._conn.execute('SELECT count(*) FROM positions').fetchone()[0] == 1
    run.db._conn.close()


@pytest.mark.parametrize('stored,expected', [(None, ''), ('10', '6'), ('6', '6'), ('20', '20')])
def test_upgrade_changes_only_old_ten_second_value_once(tmp_path, monkeypatch, stored, expected):
    import db
    monkeypatch.setenv('MATE_MARKET', 'CN')
    database = db.Database(str(tmp_path / 'migration.db'))
    database.set_setting('poll_parked', '45')
    if stored is not None:
        database.set_setting('poll_driving', stored)
    PM._migrate_cn_driving_poll(database)
    assert database.get_setting('poll_driving', '') == expected
    assert database.get_setting('poll_parked') == '45'
    database.set_setting('poll_driving', '10')
    PM._migrate_cn_driving_poll(database)
    assert database.get_setting('poll_driving') == '10'
    database._conn.close()


@pytest.mark.parametrize('seconds,samples', [(6, 10), (10, 6), (20, 6)])
def test_trip_end_confirmation_keeps_one_minute_and_minimum_evidence(monkeypatch, seconds, samples):
    monkeypatch.setenv('MATE_MARKET', 'CN')
    sm = StateMachine(poll_driving=seconds)
    data = frame(int(NOW * 1000))
    data.gear, data.speed_kmh = 'D', 30
    sm.update(data)
    assert sm.state == State.DRIVING
    data.gear, data.speed_kmh = 'P', 0
    for i in range(samples - 1):
        data.timestamp_ms += seconds * 1000
        sm.update(data)
        assert sm.state == State.DRIVING
    data.timestamp_ms += seconds * 1000
    sm.update(data)
    assert sm.state == State.PARKED_ACTIVE


def test_web_saves_and_renders_six_second_setting(tmp_path, monkeypatch):
    import db, db_reader, main
    from starlette.testclient import TestClient
    monkeypatch.setenv('MATE_MARKET', 'CN')
    path = str(tmp_path / 'web.db')
    database = db.Database(path)
    monkeypatch.setattr(db_reader, 'DB_PATH', path)
    db_reader.set_setting('setup_complete', '1')
    client = TestClient(main.app)
    response = client.post('/api/poll-settings', data={'poll_parked': '30', 'poll_driving': '6'})
    assert response.status_code == 200 and '30s / 6s' in response.text
    assert db_reader.get_setting('poll_driving') == '6'
    html = client.get('/settings').text
    assert 'name="poll_driving" min="6" max="60" step="1"' in html
    assert '<span id="pd-val">6</span>' in html
    database._conn.close()


def test_cold_cn_process_has_matching_poller_web_and_diagnostic_defaults():
    root = Path(__file__).resolve().parents[1]
    script = '''
import mate_api
from state_machine import StateMachine,DEFAULT_POLL_DRIVING
import db_reader,diagnostics
assert StateMachine().poll_driving == DEFAULT_POLL_DRIVING == 6
assert db_reader.POLL_DRIVING_DEFAULT_S == 6
assert db_reader.POLL_DRIVING_RANGE_S == (6,60)
assert diagnostics._ADVANCED_DEFAULTS['poll_driving'] == '6'
'''
    env = dict(os.environ, MATE_MARKET='CN', PYTHONPATH=os.pathsep.join([str(root/'poller'), str(root/'web')]))
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
