"""While the car charges, the Overview shows its still picture, cable in, until the charging animation has come.

The animation takes seconds to compose, and an empty card meanwhile reads as if something were broken. The page
asks for the still picture first (?still=1, the cable without its animation), and for the animation once the still
one has come or failed: with no package on disk the first request downloads it, so the second must not ask the
cloud too. The animation goes in once it has loaded, shown even where a failed still picture had left the
placeholder, and the card's refresh every 30 s brings no still frame back. Measured in a browser, because only a
browser says which picture is on screen at each frame.
Skips where it cannot run (no playwright, no Chromium), like the other browser tests.
"""
import asyncio
import base64
import datetime as dt

import pytest

pytest.importorskip("fastapi", reason="web/main.py needs fastapi (absent in the minimal CI env)")
pytest.importorskip("uvicorn", reason="the page has to be SERVED, not rendered in-process")
sync_api = pytest.importorskip("playwright.sync_api", reason="needs playwright + `playwright install chromium`")

import main  # noqa: E402
from web_in_a_browser import chromium, seed_database, served  # noqa: E402

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
_CHARGING = ("INSERT INTO positions (vehicle_id, recorded_at, soc, gear, speed_kmh, charging, plug_connected)"
             " VALUES (1, ?, 60, 'P', 0, 1, 1)")
# Which picture the card shows, on every frame from now on: its address, and whether it is drawn.
_FRAMES = ("window.pics = []; (function rec() { const i = document.querySelector('#hero-body img');"
           " window.pics.push([i.currentSrc || i.src, i.complete && i.naturalWidth > 0]); requestAnimationFrame(rec); })()")


def test_the_still_picture_is_the_cable_without_its_animation(tmp_path, monkeypatch):
    pkg = tmp_path / "car_picture_pkg.zip"
    pkg.write_bytes(b"PKcached")
    monkeypatch.setattr(main, "_car_picture_pkg_path", lambda: str(pkg))
    monkeypatch.setattr(main, "_car_pic_boot_refresh", False)
    monkeypatch.setattr(main.db_reader, "get_latest_status", lambda: {"charging": 1, "cable_connected": 0})
    monkeypatch.setattr(main.car_image, "compose", lambda _pkg, st: (repr(sorted(st.items())).encode(), "image/png"))
    main._car_image_memo.clear()
    assert asyncio.run(main.car_picture(still=1)).body == b"[('cable_connected', True), ('charging', False)]"
    assert asyncio.run(main.car_picture()).body == b"[('cable_connected', 0), ('charging', 1)]"


@pytest.fixture(scope="module")
def mate(tmp_path_factory):
    db = tmp_path_factory.mktemp("car-picture") / "leapmotor_mate.db"
    seed_database(db, "LVIN0000000000001", [(_CHARGING, (dt.datetime.now(dt.timezone.utc).isoformat(),))])
    with served(db.parent, db) as url:
        yield url


@pytest.fixture
def page(mate, request):
    """The Overview, its animation held back until the test lets it come (`page.animations` waits). The still
    picture comes at once, or answers the HTTP status the test asks for, or "held" waits in `page.stills`."""
    still = getattr(request, "param", 200)
    pw, browser = chromium(sync_api)
    p = browser.new_page()
    p.animations, p.stills = [], []

    def picture(route):
        if "still=1" not in route.request.url:
            p.animations.append(route)
        elif still == "held":
            p.stills.append(route)
        elif still == 200:
            route.fulfill(body=_PNG, content_type="image/png", headers={"Cache-Control": "max-age=300"})
        else:
            route.fulfill(status=still, body=b"")

    p.route("**/api/car-picture*", picture)
    p.goto(f"{mate}/", wait_until="domcontentloaded")     # the animation held back holds the load event too
    yield p
    browser.close()
    pw.stop()


def _shown(page):
    return page.evaluate("(() => { const i = document.querySelector('#hero-body img');"
                         " return [i.currentSrc || i.src, i.complete && i.naturalWidth > 0]; })()")


def _animation_comes(page):
    for _ in range(100):                                    # the page has asked for it
        if page.animations:
            break
        page.wait_for_timeout(50)
    for route in page.animations:
        route.fulfill(body=_PNG, content_type="image/webp", headers={"Cache-Control": "max-age=300"})
    page.animations.clear()
    page.wait_for_function("!document.querySelector('#hero-body img').src.includes('still=1')")


def test_the_still_picture_shows_until_the_animation_has_come(page):
    page.wait_for_function("document.querySelector('#hero-body img').complete")
    src, drawn = _shown(page)
    assert "still=1" in src and drawn                      # a picture at once, while the animation is made
    _animation_comes(page)
    src, drawn = _shown(page)
    assert "still=1" not in src and drawn


@pytest.mark.parametrize("page", ["held"], indirect=True)
def test_the_animation_is_asked_for_once_the_still_picture_has_come(page):
    page.wait_for_function("document.querySelector('#hero-body img')")
    page.wait_for_timeout(500)
    assert page.stills and not page.animations
    for route in page.stills:
        route.fulfill(body=_PNG, content_type="image/png")
    _animation_comes(page)


@pytest.mark.parametrize("page", [500], indirect=True)
def test_the_animation_shows_where_the_still_picture_failed(page):
    _animation_comes(page)
    assert page.evaluate("document.querySelector('#hero-body img').checkVisibility()")
    assert not page.evaluate("document.querySelector('#hero-body img').nextElementSibling.checkVisibility()")


def test_a_refresh_of_the_card_brings_no_still_frame_back(page):
    _animation_comes(page)
    page.evaluate(_FRAMES)
    page.evaluate("new Promise(ok => { document.addEventListener('htmx:afterSettle', ok, {once: true});"
                  " refreshHero(); })")
    page.wait_for_timeout(300)
    frames = page.evaluate("window.pics")
    assert frames and all("still=1" not in src and drawn for src, drawn in frames), frames
