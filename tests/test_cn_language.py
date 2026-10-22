"""Simplified Chinese is selectable, persists, and does not reset setup inputs or CN behavior."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import db
import db_reader
import i18n


@pytest.fixture
def cn_web(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    path = str(tmp_path / 'cn-language.db')
    monkeypatch.setenv('MATE_MARKET', 'CN')
    monkeypatch.setenv('DB_PATH', path)
    monkeypatch.setattr(db_reader, 'DB_PATH', path)
    monkeypatch.setattr(db_reader, '_lang_memo', [None])
    database = db.Database(path)
    yield TestClient(main.app, base_url='http://mate-cn.test'), database
    database._conn.close()


def test_simplified_locale_keeps_every_key_placeholder_and_html_tag():
    root = Path(__file__).resolve().parents[1] / 'web/locales'
    english = json.loads((root / 'en.json').read_text())['translations']
    chinese = json.loads((root / 'zh-CN.json').read_text())['translations']
    assert english.keys() == chinese.keys()
    for key, value in english.items():
        assert sorted(re.findall(r'\{[^}]+\}', value)) == sorted(re.findall(r'\{[^}]+\}', chinese[key])), key
        assert sorted(re.findall(r'</?\w+\b', value)) == sorted(re.findall(r'</?\w+\b', chinese[key])), key
    t = i18n.get_t('zh-CN')
    assert t('cn_sms_step') == '1. 短信登录'
    assert t('events_search_advanced') == '高级筛选'
    assert '数据包' in t('reev_export_ready')


def test_new_cn_install_defaults_to_simplified_and_existing_choice_is_preserved(cn_web):
    client, _ = cn_web
    assert db_reader.get_language() == 'zh-CN'
    html = client.get('/setup').text
    assert '<html lang="zh-CN"' in html and '短信登录' in html
    db_reader.set_setting('language', 'zh-TW')
    html = client.get('/setup').text
    assert '<html lang="zh-TW"' in html and '短信登入' in html
    html = client.get('/setup?language=zh-CN').text
    assert '<html lang="zh-CN"' in html
    assert db_reader.get_language() == 'zh-TW'  # viewing setup does not rewrite a saved preference


def test_both_flag_labels_and_switching_are_saved_on_settings(cn_web):
    client, _ = cn_web
    db_reader.set_setting('setup_complete', '1')
    for lang in ('zh-CN', 'zh-TW'):
        response = client.post('/api/settings/language', data={'language': lang})
        assert response.status_code == 204
        assert response.headers['HX-Refresh'] == 'true'
        assert db_reader.get_language() == lang
        html = client.get('/settings').text
        assert '🇨🇳 简体中文' in html and '🇭🇰 繁體中文' in html
        assert f'value="{lang}" selected' in html


@pytest.mark.parametrize('lang,expected', [('zh-CN', '请输入有效的中国大陆手机号码'),
                                        ('zh-TW', '請輸入有效的中國大陸手機號碼')])
def test_setup_error_uses_requested_language(cn_web, lang, expected):
    client, _ = cn_web
    response = client.post('/api/setup/sms/send', json={'phone': 'invalid', 'language': lang})
    assert response.status_code == 400 and response.json()['error'] == expected
    response = client.post('/setup', data={'language': lang, 'vehicles_json': '[]'})
    assert response.status_code == 400 and f'<html lang="{lang}"' in response.text
    assert i18n.get_t(lang)('cn_setup_invalid') in response.text


def test_simplified_numbers_and_prices_keep_dot_decimals(cn_web):
    import main
    import units
    db_reader.set_currency('CNY')
    assert units.decimal_point('50.0') == '50.0'
    assert main._dec(50, 1) == '50.0'
    assert main._money(1234.5) == '¥1,234.50'
    assert main._price_3(0.25) == '0.250'


@pytest.mark.parametrize('width', [390, 1100])
def test_setup_switches_language_without_losing_phone_capacity_or_pin(cn_web, width):
    pw = pytest.importorskip('playwright.sync_api')
    client, _ = cn_web
    asked = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def serve(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.hostname != 'mate-cn.test':
                route.abort()
                return
            path = parsed.path
            if path == '/api/setup/sms/send':
                asked.append(json.loads(request.post_data))
                route.fulfill(json={'ok': True, 'retry_in': 60})
            elif path == '/api/setup/sms/verify':
                asked.append(json.loads(request.post_data))
                route.fulfill(json={'login_id': 'synthetic', 'vehicles': [
                    {'vin': 'LFZSYNTHETIC00001', 'car_type': 'T03', 'year': 2024, 'is_shared': True}]})
            else:
                response = client.request(request.method, path + ('?' + parsed.query if parsed.query else ''),
                                          content=request.post_data_buffer, headers=request.headers)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        page.route('**/*', serve)
        page.goto('http://mate-cn.test/setup')
        assert page.locator('html').get_attribute('lang') == 'zh-CN'
        assert page.locator('#language option').all_text_contents() == ['🇨🇳 简体中文', '🇭🇰 繁體中文']
        page.locator('#phone').fill('13800000000')
        page.locator('#code').fill('123456')
        page.locator('#language').select_option('zh-TW')
        assert page.locator('#phone').input_value() == '13800000000'
        assert page.locator('#code').input_value() == '123456'
        page.locator('#send').click()
        page.wait_for_function("document.querySelector('#message').textContent.includes('驗證碼已發送')")
        page.locator('#verify').click()
        page.locator('#battery-0').wait_for()
        page.locator('#battery-0').fill('36.6')
        page.locator('#pin-0').fill('1234')
        page.locator('#language').select_option('zh-CN')
        assert page.locator('#battery-0').input_value() == '36.6'
        assert page.locator('#pin-0').input_value() == '1234'
        assert page.locator('#form-language').input_value() == 'zh-CN'
        assert '共享车辆' in page.locator('[data-car-index]').inner_text()
        assert '秒后重发' in page.locator('#send').inner_text()
        assert '登录成功，找到 1 台车辆。' in page.locator('#message').inner_text()
        serialized = page.locator('#car-form').evaluate("form => {form.onsubmit(); return JSON.parse(document.querySelector('#cars-json').value)}")
        assert serialized[0]['battery'] == '36.6' and serialized[0]['pin'] == '1234'
        assert all(body['language'] == 'zh-TW' for body in asked) and len(asked) == 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
