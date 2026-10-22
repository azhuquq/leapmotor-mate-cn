"""Car-picture cache refresh (#143). The per-vehicle image package is cached on disk, but a car's
colour can change (or be provisional right after registration) — TripelJ's new B10 reads purple in the
cloud/official app yet Mate showed the white it had cached at first setup. So Mate re-downloads the
package ONCE after each (re)start, keeping the cached image if the cloud is unreachable at that moment.
With a copy on disk that download runs alongside: the Overview shows the copy at once rather than an empty
card for as long as the cloud takes. A manual ?refresh=1 always re-downloads, and waits for it. It is NOT
a continuous refresh — steady-state serves the cache.
"""
import asyncio
import threading

import pytest

pytest.importorskip("fastapi", reason="web.main needs fastapi")
import main


def _wire(tmp_path, monkeypatch, *, cloud_returns=b"PKfresh", boot=True):
    pkg_path = str(tmp_path / "car_picture_pkg.zip")
    with open(pkg_path, "wb") as f:
        f.write(b"PKcached")                       # a pre-existing on-disk cache
    monkeypatch.setattr(main, "_car_picture_pkg_path", lambda: pkg_path)
    monkeypatch.setattr(main, "_car_pic_boot_refresh", boot)
    main._car_image_memo.clear()
    calls = {"n": 0}

    def _dl():
        calls["n"] += 1
        return cloud_returns

    monkeypatch.setattr(main.command_client, "get_car_picture_package", _dl)
    monkeypatch.setattr(main.db_reader, "get_latest_status", lambda: {})
    seen = {}

    def _compose(pkg, status):
        seen["pkg"] = pkg                          # which bytes actually got composed/served
        return (b"img", "image/png")

    monkeypatch.setattr(main.car_image, "compose", _compose)
    return pkg_path, calls, seen


def test_refreshes_once_after_boot_then_uses_cache(tmp_path, monkeypatch):
    _, calls, seen = _wire(tmp_path, monkeypatch)
    cloud, written = threading.Event(), threading.Event()
    download = main._download_car_picture_package

    def _dl():
        calls["n"] += 1
        return b"PKfresh" if cloud.wait(10) else None   # the cloud answers once the test lets it

    def _download(pkg_path):
        try:
            return download(pkg_path)
        finally:
            written.set()

    monkeypatch.setattr(main.command_client, "get_car_picture_package", _dl)
    monkeypatch.setattr(main, "_download_car_picture_package", _download)

    async def requests():                          # one loop: closing it waits for the download thread
        try:
            await main.car_picture()               # first request after a (re)start → the copy, a re-download alongside
            assert calls["n"] == 1 and seen["pkg"] == b"PKcached"
        finally:
            cloud.set()
        assert written.wait(10)
        await main.car_picture()                   # subsequent requests → the fresh package from disk, no new download
        assert calls["n"] == 1 and seen["pkg"] == b"PKfresh"

    asyncio.run(requests())


def test_the_first_picture_after_a_restart_does_not_wait_for_the_cloud(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    cloud = threading.Event()
    monkeypatch.setattr(main.command_client, "get_car_picture_package", lambda: cloud.wait(10) and b"PKfresh")

    async def first_request():
        try:
            return await asyncio.wait_for(main.car_picture(), 2)
        finally:
            cloud.set()                            # the cloud answers only after the picture was served

    assert asyncio.run(first_request()).body == b"img"


def test_boot_refresh_keeps_cache_when_cloud_down(tmp_path, monkeypatch):
    _, calls, seen = _wire(tmp_path, monkeypatch, cloud_returns=None)   # cloud unreachable
    asyncio.run(main.car_picture())
    assert calls["n"] == 1                         # it tried the refresh once…
    assert seen["pkg"] == b"PKcached"             # …then kept the cached image (never goes blank)


def test_steady_state_serves_cache_without_download(tmp_path, monkeypatch):
    _, calls, seen = _wire(tmp_path, monkeypatch, boot=False)
    asyncio.run(main.car_picture())
    assert calls["n"] == 0 and seen["pkg"] == b"PKcached"   # no continuous polling


def test_manual_refresh_always_redownloads(tmp_path, monkeypatch):
    _, calls, seen = _wire(tmp_path, monkeypatch, boot=False)
    asyncio.run(main.car_picture(refresh=1))
    assert calls["n"] == 1 and seen["pkg"] == b"PKfresh"
