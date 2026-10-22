"""The chart of the charge in progress is redrawn with every poll of its panel, and the swap drops the
element the chart was drawn on. ApexCharts hangs a resize listener on the window for every instance
and lets go of it only in destroy(), so a chart that is dropped without one lives on in memory for as
long as the page is open: a night's charge is over a thousand of them. The panel destroys its chart
before every swap, the one that empties it when the charge ends included; a poll that fails swaps
nothing, and the chart it would have replaced stays.

The new chart is drawn in the step that swaps it in. htmx runs a swapped-in script when it settles the
swap, 20 ms later by default, and the browser painted in between: the card without its chart, and the
page under it jumping up by the chart's height and back, at every poll.

Measured in a real browser, like the other chart tests; skips where it cannot run.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi", reason="web/main.py needs fastapi (absent in the minimal CI env)")
pytest.importorskip("uvicorn", reason="the page has to be SERVED, not rendered in-process")
sync_api = pytest.importorskip("playwright.sync_api", reason="needs playwright + `playwright install chromium`")

from web_in_a_browser import chromium, seed_database, served

VIN = "LVIN0000000000001"
PLUGGED_IN = datetime(2026, 10, 2, 6, 13, tzinfo=timezone.utc)


def _a_charge_in_progress():
    rows = [("INSERT INTO settings (key, value) VALUES ('timezone', 'UTC')", ()),
            ("INSERT INTO charges (id, vehicle_id, started_at, start_soc) VALUES (9, 1, ?, 77.1)",
             (PLUGGED_IN.isoformat(),))]
    sample = ("INSERT INTO positions (vehicle_id, recorded_at, charging, charge_voltage_v, charge_current_a, soc)"
              " VALUES (1, ?, 1, 430, -7, ?)")
    for k in range(6):
        rows.append((sample, ((PLUGGED_IN + timedelta(seconds=30 * k)).isoformat(), 77.1 + 0.1 * k)))
    return rows


@pytest.fixture
def mate(tmp_path_factory):
    data = tmp_path_factory.mktemp("mate-live-chart")
    db = data / "leapmotor_mate.db"
    seed_database(db, VIN, _a_charge_in_progress())
    with served(data, db) as url:
        yield url, db


# Counts the resize listeners the page adds to and removes from the window.
_COUNT = """(() => {
  window.__resize = { add: 0, rm: 0 };
  const add = window.addEventListener.bind(window), rm = window.removeEventListener.bind(window);
  window.addEventListener = (t, f, o) => { if (t === 'resize') window.__resize.add++; return add(t, f, o); };
  window.removeEventListener = (t, f, o) => { if (t === 'resize') window.__resize.rm++; return rm(t, f, o); };
})()"""
_DRAWN = "() => { const e = document.querySelector('#charging-chart-panel [id^=pc-]'); return e && e._c && e.querySelector('.apexcharts-canvas'); }"
_SWAP = "() => htmx.ajax('GET', 'api/charging-chart', { target: '#charging-chart-panel', swap: 'innerHTML' })"
_FAILED = "() => htmx.ajax('GET', 'api/charging-chart/nowhere', { target: '#charging-chart-panel', swap: 'innerHTML' })"
_CANVASES = "() => document.querySelectorAll('.apexcharts-canvas').length"
# A poll as the panel is set up for, looked at right after the swap's own step: a timer set then fires
# before any the swap leaves behind.
_DRAWN_AFTER_ITS_SWAP = """() => new Promise(done => {
  const panel = document.getElementById('charging-chart-panel');
  panel.addEventListener('htmx:afterSwap', () => setTimeout(() => done(!!(%s)()), 0), { once: true });
  htmx.ajax('GET', 'api/charging-chart', { source: panel });
})""" % _DRAWN


def _alive(page):
    return page.evaluate("() => window.__resize.add - window.__resize.rm")


def test_a_redrawn_chart_does_not_outlive_its_element(mate):
    url, db = mate
    pw, browser = chromium(sync_api)
    try:
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(_COUNT)
        assert page.goto(f"{url}/charges").status == 200
        page.wait_for_function(_DRAWN)
        drawn_once = _alive(page)

        for _ in range(3):
            page.evaluate(_SWAP)
            page.wait_for_function(_DRAWN)
        assert _alive(page) == drawn_once, "every redraw left the previous chart's listener behind"
        assert page.evaluate(_CANVASES) == 1

        page.evaluate(_FAILED)   # a poll that fails: nothing is swapped, so nothing may be destroyed
        assert page.evaluate(_CANVASES) == 1 and _alive(page) == drawn_once, \
            "a failed poll took the chart down although its element stayed"
        page.evaluate(_SWAP)
        page.wait_for_function(_DRAWN)
        assert _alive(page) == drawn_once and page.evaluate(_CANVASES) == 1

        con = sqlite3.connect(db)
        con.execute("UPDATE charges SET ended_at = ?, end_soc = 77.6 WHERE id = 9",
                    ((PLUGGED_IN + timedelta(minutes=3)).isoformat(),))
        con.commit()
        con.close()
        page.evaluate(_SWAP)
        page.wait_for_function("() => !document.querySelector('#charging-chart-panel .card')")
        assert _alive(page) < drawn_once, "the chart of the charge that ended was not destroyed"
        assert page.evaluate(_CANVASES) == 0
        assert errors == []
    finally:
        browser.close()
        pw.stop()


def test_a_redrawn_chart_is_in_place_before_the_browser_paints(mate):
    url, _ = mate
    pw, browser = chromium(sync_api)
    try:
        page = browser.new_page()
        assert page.goto(f"{url}/charges").status == 200
        page.wait_for_function(_DRAWN)
        assert page.evaluate(_DRAWN_AFTER_ITS_SWAP), "the card was swapped in without its chart"
    finally:
        browser.close()
        pw.stop()
