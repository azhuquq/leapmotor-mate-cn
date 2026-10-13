"""VIN-specific appearance images, following ha-leapmotor-cn's image/coordinator flow.

Keep the signed URL server-side, refresh metadata every six hours, and download
only when its URL changes. Failures retain the last image and retry in five minutes.
"""
import logging
import threading
import time
from dataclasses import dataclass

from leapmotor_cn.protocol import parse_car_picture_url

log = logging.getLogger(__name__)
REFRESH_SECONDS = 21600
RETRY_SECONDS = 300


@dataclass
class Picture:
    url: str = ""
    image: bytes | None = None
    refresh_at: float = 0


class CarPictureCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._account = ""
        self._pictures: dict[str, Picture] = {}
        self._lock = threading.Lock()

    def clear(self):
        with self._lock:
            self._account = ""
            self._pictures.clear()

    def get(self, account, vin, metadata, download, *, refresh=False):
        if not account or not vin:
            return None
        with self._lock:
            if account != self._account:
                self._pictures.clear()
                self._account = account
            picture = self._pictures.setdefault(vin.lower(), Picture())
            now = self._clock()
            if not refresh and now < picture.refresh_at:
                return picture.image
            picture.refresh_at = now + RETRY_SECONDS
            try:
                url = parse_car_picture_url(metadata())
                if not url:
                    return picture.image
                if url == picture.url and picture.image and not refresh:
                    picture.refresh_at = now + REFRESH_SECONDS
                    return picture.image
                image = download(url)
                if not image:
                    return picture.image
            except Exception as error:
                # Transport errors can include a signed URL; log only the type.
                log.warning("CN vehicle image unavailable (%s); retaining cache", type(error).__name__)
                return picture.image
            picture.url, picture.image = url, image
            picture.refresh_at = now + REFRESH_SECONDS
            return picture.image
