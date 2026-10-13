"""CN appearance images reach the web UI without ZIPs, repeated downloads or mixed cars."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import pytest
import mate_api
from cn_car_picture import CarPictureCache, REFRESH_SECONDS, RETRY_SECONDS
from leapmotor_cn.protocol import parse_car_picture_url


@pytest.mark.parametrize("field", ["shareBindUrl", "url", "imageUrl", "picUrl", "carPic"])
def test_reference_url_fields_and_https_upgrade(field):
    assert parse_car_picture_url({"data": {field: "http://synthetic.oss-cn-hangzhou.aliyuncs.com/car.png"}}) == "https://synthetic.oss-cn-hangzhou.aliyuncs.com/car.png"


def test_url_priority_prefers_the_whole_car_image():
    assert parse_car_picture_url({"data": {"shareBindUrl": "https://example.test/whole.png",
        "url": "https://example.test/part.png"}}) == "https://example.test/whole.png"


def test_metadata_ttl_same_url_and_changed_url():
    now = [0]
    cache = CarPictureCache(clock=lambda: now[0])
    calls = []
    url = ["https://example.test/one.png"]
    def metadata():
        calls.append("metadata")
        return {"data": {"shareBindUrl": url[0]}}
    def download(at):
        calls.append(at)
        return at.encode()
    first = cache.get("account", "CAR-A", metadata, download)
    for _ in range(5):
        assert cache.get("account", "car-a", metadata, download) == first
    assert calls == ["metadata", url[0]]
    now[0] += REFRESH_SECONDS
    assert cache.get("account", "CAR-A", metadata, download) == first
    assert calls == ["metadata", url[0], "metadata"]
    url[0] = "https://example.test/two.png"
    now[0] += REFRESH_SECONDS
    assert cache.get("account", "CAR-A", metadata, download) == url[0].encode()
    assert calls[-2:] == ["metadata", url[0]]


@pytest.mark.parametrize("failure", ["metadata", "empty_url", "download"])
def test_failures_keep_the_old_image_and_retry_metadata(failure, caplog):
    now = [0]
    cache = CarPictureCache(clock=lambda: now[0])
    metadata_calls = []
    def metadata():
        metadata_calls.append(1)
        if failure == "metadata":
            raise RuntimeError("https://example.test/private?signature=PRIVATE")
        return {} if failure == "empty_url" else {"data": {"url": "https://example.test/new.png"}}
    def fail_download(_):
        raise RuntimeError("https://example.test/private?signature=PRIVATE")
    cache.get("account", "CAR-A", lambda: {"data": {"url": "https://example.test/old.png"}}, lambda _: b"old")
    now[0] += REFRESH_SECONDS
    assert cache.get("account", "CAR-A", metadata, fail_download) == b"old"
    assert cache.get("account", "CAR-A", metadata, fail_download) == b"old"
    assert len(metadata_calls) == 1
    now[0] += RETRY_SECONDS
    assert cache.get("account", "CAR-A", lambda: {"data": {"url": "https://example.test/new.png"}}, lambda _: b"new") == b"new"
    assert "PRIVATE" not in caplog.text


def test_no_image_failure_is_throttled_and_manual_refresh_bypasses_wait():
    cache = CarPictureCache(clock=lambda: 0)
    calls = []
    def metadata():
        calls.append(1)
        return {}
    for _ in range(3):
        assert cache.get("account", "CAR-A", metadata, lambda _: b"unused") is None
    assert len(calls) == 1
    assert cache.get("account", "CAR-A", lambda: {"data": {"url": "https://example.test/car.png"}}, lambda _: b"png", refresh=True) == b"png"


def test_cache_isolated_by_car_and_account_and_clear():
    cache = CarPictureCache()
    calls = []
    def get(account, vin):
        return cache.get(account, vin, lambda: {"data": {"url": "https://example.test/" + vin}},
                         lambda _: (calls.append((account, vin)), (account + vin).encode())[1])
    assert get("one", "CAR-A") == b"oneCAR-A"
    assert get("one", "CAR-B") == b"oneCAR-B"
    assert get("one", "CAR-A") == b"oneCAR-A"
    assert len(calls) == 2
    assert get("two", "CAR-A") == b"twoCAR-A"
    cache.clear()
    assert get("two", "CAR-A") == b"twoCAR-A"
    assert len(calls) == 4


def test_concurrent_requests_download_once():
    cache = CarPictureCache()
    calls = []
    def get(_):
        return cache.get("account", "CAR-A", lambda: {"data": {"url": "https://example.test/car.png"}},
                         lambda _: (calls.append(1), b"png")[1])
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(get, range(8))) == [b"png"] * 8
    assert calls == [1]


def test_session_fetches_the_requested_car_without_reset_or_package(monkeypatch):
    import command_client, db_reader
    monkeypatch.setenv("MATE_MARKET", "CN")
    monkeypatch.setattr(db_reader, "get_account_user", lambda: "synthetic-account")
    cars = [SimpleNamespace(vin="CAR-A"), SimpleNamespace(vin="CAR-B")]
    calls = []
    session = command_client.LeapmotorSession()
    session._vehicles = cars
    session._api = SimpleNamespace(get_car_picture=lambda car: (calls.append(car.vin),
        {"data": {"shareBindUrl": "https://example.test/" + car.vin}})[1],
        download_picture=lambda url: url.encode())
    monkeypatch.setattr(session, "_connect", lambda: None)
    monkeypatch.setattr(session, "_reset", lambda: pytest.fail("image failure must not reset login"))
    assert session.get_car_picture(vin="CAR-B") == b"https://example.test/CAR-B"
    assert session.get_car_picture(vin="CAR-B") == b"https://example.test/CAR-B"
    assert session.get_car_picture(vin="NOT-ON-ACCOUNT") is None
    assert calls == ["CAR-B"]


def test_web_endpoint_uses_png_and_pins_the_car_not_the_package(monkeypatch):
    import main
    monkeypatch.setenv("MATE_MARKET", "CN")
    cars = [{"id": 1, "vin": "CAR-A"}, {"id": 2, "vin": "CAR-B"}]
    monkeypatch.setattr(main.db_reader, "get_account_user", lambda: "synthetic-account")
    monkeypatch.setattr(main.db_reader, "get_vehicle", lambda: (cars[0], None))
    monkeypatch.setattr(main.db_reader, "get_vehicles", lambda: cars)
    calls = []
    monkeypatch.setattr(main.command_client, "get_car_picture", lambda **kw: (calls.append(kw), b"png")[1])
    monkeypatch.setattr(main.command_client, "get_car_picture_package", lambda: pytest.fail("CN must not fetch a ZIP"))
    response = asyncio.run(main.car_picture(vehicle_id=2, refresh=1))
    assert response.body == b"png" and response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert calls == [{"vin": "CAR-B", "refresh": True}]
    assert asyncio.run(main.car_picture(vehicle_id=999)).status_code == 404


def test_web_does_not_return_an_old_account_image_after_logout(monkeypatch):
    import main
    monkeypatch.setenv("MATE_MARKET", "CN")
    account = ["synthetic-account"]
    monkeypatch.setattr(main.db_reader, "get_account_user", lambda: account[0])
    monkeypatch.setattr(main.db_reader, "get_vehicle", lambda: ({"id": 1, "vin": "CAR-A"}, None))
    def download(**kw):
        account[0] = ""
        return b"old-account-image"
    monkeypatch.setattr(main.command_client, "get_car_picture", download)
    assert asyncio.run(main.car_picture()).status_code == 404


def test_cdn_download_has_no_account_headers_and_converts_to_png(monkeypatch):
    from PIL import Image
    from cn_backend import ChinaAPIClient
    import urllib.request
    image = BytesIO()
    Image.new("RGB", (4, 4), "green").save(image, format="JPEG")
    calls = []
    class Opener:
        def open(self, url, timeout):
            calls.append((url, timeout))
            return BytesIO(image.getvalue())
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    client = ChinaAPIClient()
    png = client.download_picture("https://synthetic.oss-cn-hangzhou.aliyuncs.com/car.jpg")
    assert png.startswith(b"\x89PNG")
    assert calls == [("https://synthetic.oss-cn-hangzhou.aliyuncs.com/car.jpg", 20)]


@pytest.mark.parametrize("url", ["http://example.test/car.png", "https://evil.test/car.png",
    "https://aliyuncs.com.evil.test/car.png", "https://name:secret@leapmotor.com/car.png"])
def test_invalid_cdn_url_never_downloads(url, monkeypatch):
    from cn_backend import ChinaAPIClient, ChinaError
    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: pytest.fail("invalid URL reached network"))
    with pytest.raises(ChinaError):
        ChinaAPIClient().download_picture(url)
