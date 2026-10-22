"""MATE_DEMO must be fully opt-in: off by default, and demo.install() a no-op unless the
flag is set — so a normal install is never altered by the demo code being present."""
import types

import demo


def test_is_demo_off_by_default(monkeypatch):
    monkeypatch.delenv("MATE_DEMO", raising=False)
    assert demo.is_demo() is False
    for v in ("", "0", "false", "False", "no"):
        monkeypatch.setenv("MATE_DEMO", v)
        assert demo.is_demo() is False


def test_is_demo_on(monkeypatch):
    monkeypatch.setenv("MATE_DEMO", "1")
    assert demo.is_demo() is True


def test_install_is_noop_when_off(monkeypatch):
    monkeypatch.delenv("MATE_DEMO", raising=False)
    cc = types.SimpleNamespace(
        _session=types.SimpleNamespace(execute=lambda *a, **k: ("real", "real")),
        get_fresh_signals=lambda *a, **k: "REAL")
    ha = types.SimpleNamespace(is_configured=lambda *a, **k: "REAL")
    demo.install(cc, ha)                      # flag off → must change nothing
    assert cc.get_fresh_signals() == "REAL"
    assert cc._session.execute() == ("real", "real")
    assert ha.is_configured() == "REAL"


def test_install_patches_when_on(monkeypatch):
    monkeypatch.setenv("MATE_DEMO", "1")
    cc = types.SimpleNamespace(
        _session=types.SimpleNamespace(execute=lambda *a, **k: ("real", "real")),
        get_fresh_signals=lambda *a, **k: "REAL")
    demo.install(cc)                          # flag on → cloud calls neutered
    assert cc.get_fresh_signals() is None
    assert cc.get_diagnostic_signals() is None
    assert cc._session.execute() == (True, "demo")


# ── In-app demo toggle (flag file read by run.sh at boot) ──────────────────────

def test_flag_path_next_to_db(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "leapmotor_mate.db"))
    assert demo.flag_path() == str(tmp_path / "demo.flag")


def test_set_flag_writes_and_removes(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "leapmotor_mate.db"))
    flag = tmp_path / "demo.flag"
    assert not flag.exists()
    demo.set_flag(True)
    assert flag.exists()
    demo.set_flag(False)
    assert not flag.exists()
    demo.set_flag(False)          # idempotent — removing a missing flag must not raise


def test_flag_path_identical_in_normal_and_demo_db(monkeypatch, tmp_path):
    # run.sh derives the flag from the NORMAL DB_PATH; the in-demo exit button runs with the
    # demo DB_PATH. Both must resolve to the same file or exit would not clear what boot reads.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "leapmotor_mate.db"))
    normal = demo.flag_path()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "demo.db"))
    assert demo.flag_path() == normal
