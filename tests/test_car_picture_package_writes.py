"""Background and manual picture refreshes must each publish a complete ZIP."""
import io
import os
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import main


def _package(content):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        archive.writestr('picture.png', content)
    return result.getvalue()


def test_overlapping_refreshes_never_modify_an_already_published_package(tmp_path, monkeypatch):
    path = tmp_path / 'car_picture_pkg.zip'
    old, new = _package(b'old'), _package(b'new picture with more layers')
    first_opened, second_replaced = threading.Event(), threading.Event()
    worker = threading.local()
    real_open, real_replace = io.open, os.replace

    class DelayedWrite:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, key):
            return getattr(self.file, key)

        def __enter__(self):
            self.file.__enter__()
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def write(self, content):
            # The first writer has opened its staging file; let the second publish
            # before the first writes. With shared .tmp, that first file is now live.
            first_opened.set()
            assert second_replaced.wait(5)
            return self.file.write(content)

    def controlled_open(*args, **kwargs):
        file = real_open(*args, **kwargs)
        return DelayedWrite(file) if getattr(worker, 'first', False) else file

    def replace(src, dest):
        real_replace(src, dest)
        if not worker.first:
            second_replaced.set()

    def download(first):
        worker.first = first
        return main._download_car_picture_package(str(path))

    monkeypatch.setattr(main.command_client, 'get_car_picture_package', lambda: old if worker.first else new)
    # Cover ordinary open and tempfile's opener; all I/O and replacements are real.
    monkeypatch.setattr(main, 'open', controlled_open, raising=False)
    monkeypatch.setattr(tempfile._io, 'open', controlled_open)
    monkeypatch.setattr(main.os, 'replace', replace)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(download, True)
        try:
            assert first_opened.wait(5)
            second = pool.submit(download, False)
            assert second.result(timeout=5) == new
        finally:
            second_replaced.set()
        assert first.result(timeout=5) == old
    assert path.read_bytes() in (old, new)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
    assert list(tmp_path.iterdir()) == [path]


def test_failed_replacement_keeps_the_previous_package_and_removes_staging(tmp_path, monkeypatch):
    path = tmp_path / 'car_picture_pkg.zip'
    old, new = _package(b'old'), _package(b'new')
    path.write_bytes(old)
    monkeypatch.setattr(main.command_client, 'get_car_picture_package', lambda: new)

    def refuse(*args):
        raise OSError('synthetic filesystem refusal')

    monkeypatch.setattr(main.os, 'replace', refuse)
    assert main._download_car_picture_package(str(path)) == new
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]
