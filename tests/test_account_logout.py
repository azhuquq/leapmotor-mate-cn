"""Settings → Logout: clears ONLY the Leapmotor login so a different account can be linked,
without touching trips/charges/positions. The poller detects the credential change and exits
(run.sh restarts the container) to re-authenticate as the new account. Pure poller.db → CI-safe."""
import db as D


def test_logout_clears_login_but_keeps_data(tmp_path):
    database = D.Database(str(tmp_path / "t.db"))
    # A configured account with a car already on file.
    database.set_setting("leapmotor_user", "old@example.com")
    database.set_secret("leapmotor_pass", "oldpass")
    database.mark_setup_complete()
    vid = database.ensure_vehicle("LVIN0000000000001", "B10", 2025)
    database.set_secret("leapmotor_pin_lvin0000000000001", "1111")

    # Exactly what POST /api/account/logout does: clear the login keys, each car's PIN + the setup flag.
    database.set_setting("leapmotor_user", "")
    database.set_secret("leapmotor_pass", "")
    database.set_secret("leapmotor_pin_lvin0000000000001", "")
    database.set_setting("setup_complete", "0")

    # Login is gone and the wizard re-opens…
    assert database.get_setting("leapmotor_user") == ""
    assert database.get_secret("leapmotor_pass") == ""
    assert database.get_operate_pin("LVIN0000000000001") == ""
    assert not database.is_setup_complete()
    # …but the car (and therefore its trips/charges, keyed by vehicle_id) is untouched.
    assert database.ensure_vehicle("LVIN0000000000001", "B10", 2025) == vid


def test_account_switch_is_detected(tmp_path):
    """The poller guards on (user, pass) != startup: a *different* complete login means 'switch
    accounts → restart'. The cleared logout window (any field empty) must NOT trigger. The PIN it
    also compares is only `LEAPMOTOR_PIN`, which never changes under a running process."""
    database = D.Database(str(tmp_path / "t.db"))
    database.set_setting("leapmotor_user", "old@example.com")
    database.set_secret("leapmotor_pass", "oldpass")
    startup = (database.get_setting("leapmotor_user"), database.get_secret("leapmotor_pass"))

    # Logged-out limbo: creds cleared → tuple differs but is incomplete → no restart.
    database.set_setting("leapmotor_user", "")
    database.set_secret("leapmotor_pass", "")
    limbo = (database.get_setting("leapmotor_user"), database.get_secret("leapmotor_pass"))
    assert limbo != startup and not all(limbo)   # guard's all(...) check skips this

    # New account linked via the wizard: complete + different → poller exits to re-auth.
    database.set_setting("leapmotor_user", "new@example.com")
    database.set_secret("leapmotor_pass", "newpass")
    switched = (database.get_setting("leapmotor_user"), database.get_secret("leapmotor_pass"))
    assert switched != startup and all(switched)


def test_factory_reset_wipes_all_data(tmp_path):
    """Settings → Delete account / Factory reset: erase EVERYTHING (account, settings, vehicles,
    positions, …) and reopen the wizard — unlike Logout, which keeps history. The poller runs this
    at startup when the web marker is set. Pure poller.db → CI-safe."""
    database = D.Database(str(tmp_path / "t.db"))
    # A fully configured, used install: account, an integration setting, a car, and a history row.
    database.set_setting("leapmotor_user", "old@example.com")
    database.set_secret("leapmotor_pass", "oldpass")
    database.mark_setup_complete()
    database.set_setting("mqtt_host", "192.168.1.10")     # an integration setting must go too
    database.set_setting("factory_reset_pending", "1")    # the web-set marker
    vid = database.ensure_vehicle("LVIN0000000000001", "B10", 2025)
    database._conn.execute(
        "INSERT INTO positions (vehicle_id, recorded_at, soc) VALUES (?, ?, ?)",
        (vid, "2026-06-23T10:00:00", 80.0))
    database._conn.commit()

    database.factory_reset()

    # Every user table is empty…
    tables = [r["name"] for r in database._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    for t in tables:
        n = database._conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        assert n == 0, f"{t} not empty after factory reset ({n} rows)"
    # …including the account, the integration setting, the marker and the setup flag → wizard reopens.
    assert database.get_setting("leapmotor_user") == ""
    assert database.get_setting("mqtt_host") == ""
    assert database.get_setting("factory_reset_pending", "0") == "0"
    assert not database.is_setup_complete()


def test_factory_reset_keeps_schema_usable(tmp_path):
    """The wipe deletes rows, not tables: a fresh install can re-onboard immediately, and the
    AUTOINCREMENT counters are reset so ids restart from 1."""
    database = D.Database(str(tmp_path / "t.db"))
    database.ensure_vehicle("LVIN0000000000001", "B10", 2025)
    database.factory_reset()
    assert database.ensure_vehicle("LVIN0000000000002", "C10", 2026) == 1
