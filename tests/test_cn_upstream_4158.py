"""Hold the upstream 4.15.8 changes to the China backend's actual semantics."""
import io
import json
import dataclasses

import pytest
import mate_api  # noqa: F401 — install the shared runtime import path
import cn_backend
import db_reader
import diagnostics
from cn_telemetry import decode_status
from test_cn_adapter import api, raw, vehicle  # noqa: F401
from test_the_mqtt_plug_sensor_shows_the_cable_the_pages_show import _published


@pytest.mark.parametrize('code,port,gear,speed,expected', [
    (0, 1, 0, 0, ('OFF', 'false')), (1, 1, 0, 0, ('ON', 'true')),
    (2, 0, 0, 0, ('ON', 'true')), (4, 0, 0, 0, ('ON', 'true')),
    (6, 0, 0, 0, ('ON', 'true')), (9, 1, 0, 0, ('', '')),
    (1, 1, 3, 40, ('OFF', 'false')),
])
def test_cn_mqtt_uses_cn_plug_states_and_keeps_unknown_unknown(code, port, gear, speed, expected):
    data = decode_status(raw(**{'1149': code, '47': port, '1010': gear, '1319': speed}), vehicle()).data
    assert _published(dataclasses.replace(data, vin='VINTEST')) == expected


def test_cn_real_transport_records_codes_without_request_or_response_secrets(api, monkeypatch):
    monkeypatch.setattr(db_reader, 'DB_PATH', str(cn_backend.db_path()))
    answers = iter([{'code': 0, 'data': {'accessToken': 'synthetic-secret'}}, {'code': 200},
                    {'code': 40}, {'result': 'synthetic-secret-message'}])
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(json.dumps(next(answers)).encode())
    monkeypatch.setattr(cn_backend.urllib.request, 'build_opener', lambda *args: Opener())
    base = 'https://appgateway.leapmotor.com'
    paths = ['/base/base-user/account/v1/login', '/app-user/appuseroperate/getnewtoken',
             '/app/app-signal-service/signal/info/query', '/carownerservice/v3/api/carpicture/key']
    for path in paths:
        cn_backend.http_request('GET', base + path + '?vin=SYNTHETIC-VIN&token=synthetic-query-secret',
                                {'token': 'synthetic-header-secret'})
    text = diagnostics._cloud_answers_section()
    assert '4 requests' in text and '200 [40]' in text and 'invalid_code' in text
    listed = text.split('at most 200):', 1)[1]
    assert '/base/base-user/account/v1/login' in listed
    assert '/app-user/appuseroperate/getnewtoken' in listed
    for secret in ('SYNTHETIC-VIN', 'synthetic-secret', 'synthetic-query-secret', 'synthetic-header-secret'):
        assert secret not in text
    cn_backend.audit_response('GET', base + '/app-user/appuserinfo/compliance/queryuserinfo', 200, [200])
    listed = diagnostics._cloud_answers_section().split('at most 200):', 1)[1]
    assert '/app-user/appuserinfo/compliance/queryuserinfo' not in listed


def test_cn_transport_failures_are_recorded_without_exception_text(api, monkeypatch):
    monkeypatch.setattr(db_reader, 'DB_PATH', str(cn_backend.db_path()))
    class Opener:
        def open(self, request, timeout):
            raise OSError('synthetic-sensitive-network-details')
    monkeypatch.setattr(cn_backend.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(cn_backend.ChinaError):
        cn_backend.http_request('GET', 'https://appgateway.leapmotor.com/test', {})
    text = diagnostics._cloud_answers_section()
    assert 'transport_error' in text and 'synthetic-sensitive' not in text
