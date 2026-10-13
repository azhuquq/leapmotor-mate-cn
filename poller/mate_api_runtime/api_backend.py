"""Production uses the China adapter; EU is retained for upstream regression fixtures."""
import os


def is_china():
    return os.environ.get("MATE_MARKET", "CN").upper() == "CN"


if is_china():
    from cn_backend import ChinaAPIClient as LeapmotorApiClient
else:
    from api_v2_bridge import NewAPIClient as LeapmotorApiClient
