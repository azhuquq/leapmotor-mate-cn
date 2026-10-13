"""China adapter: one encrypted session across processes, passive reads and explicit commands.

Authentication/payload primitives are vendored from ha-leapmotor-cn (AGPLv3).
B01 control form/signStr and scalar receipts follow app2api's documented protocol.
"""
import base64
import copy
import dataclasses
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import crypto
from process_lock import exclusive
from leapmotor_cn import protocol as p
from leapmotor_cloud.mate_compat import Vehicle, MateAPIError
from cn_capabilities import permission

SESSION_KEY = 'cn_shared_session'
GLOBAL = 'https://app-gw-global-master.leapmotor.com'
CONTROL = '/app/app-control-service/v3/api/appremotectl'
# Signature refusals (39/302002002/302010205) do not invalidate a session.
AUTH_CODES = set(p.AUTH_ERROR_CODES) | {45}
PROFILE = {'device_type': 'android', 'version': p.APP_VERSION, 'sub_version': p.APP_SUBVERSION}
SESSION_FIELDS = ('token', 'refresh_token', 'phone', 'car_token', 'car_refresh_token',
                  'sign_param', 'encrypt_param', 'user_id', 'device_id',
                  'account_token_expires_at', 'version', 'sub_version')


class ChinaError(MateAPIError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.api_codes = () if code is None else (code,)
        self.code = code


class ChinaAuthRequired(ChinaError): pass
class UnsupportedFeature(ChinaError): pass


def db_path():
    return Path(os.environ.get('DB_PATH', 'leapmotor_mate.db')).resolve()


def connect():
    db = sqlite3.connect(db_path(), timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    return db


def get_setting(key, default=''):
    with connect() as db:
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))


def phone_number(value):
    value = str(value).strip().replace(' ', '').replace('-', '')
    for prefix in ('+86', '0086'):
        if value.startswith(prefix): value = value[len(prefix):]; break
    if not re.fullmatch(r'1[0-9]{10}', value): raise ChinaError('請輸入有效的中國大陸手機號碼')
    return value


def origin(value):
    parsed = urlsplit(str(value))
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
        or parsed.query or parsed.fragment or parsed.path not in ('', '/')
        or not any(host == suffix or host.endswith('.' + suffix) for suffix in ('leapmotor.com', 'leapmotor.cn'))):
        raise ChinaError('中國區車輛路由無效')
    return 'https://' + host


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def http_request(method, url, headers, body=None, form=False):
    parts = urlsplit(url)
    origin(parts.scheme + '://' + parts.netloc)
    if body is not None and not isinstance(body, bytes):
        body = (urlencode(body) if form else json.dumps(body, ensure_ascii=False, separators=(',', ':'))).encode()
        headers = dict(headers, **{'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'})
    request = urllib.request.Request(url, body, headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    try:
        with opener.open(request, timeout=25) as response:
            raw = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        if exc.code == 401: raise ChinaAuthRequired('中國區車端登入資料已失效', 401) from None
        raise ChinaError('中國區服務 HTTP %s' % exc.code, exc.code) from None
    except (OSError, urllib.error.URLError):
        raise ChinaError('中國區服務暫時無法連線，已保留登入資料') from None
    if len(raw) > 2_000_000: raise ChinaError('中國區回應超過大小限制')
    try: result = json.loads(raw)
    except (ValueError, UnicodeError): raise ChinaError('中國區服務回應格式無效') from None
    if not isinstance(result, dict): raise ChinaError('中國區服務回應格式無效')
    return result


def check(result, legacy=False):
    if not isinstance(result, dict): raise ChinaError('中國區服務回應格式無效')
    code = result.get('code', result.get('result'))
    if str(code) not in (('0', '200') if legacy else ('0',)):
        try: code = int(code)
        except (ValueError, TypeError): code = None
        text = {4: '車輛操作 PIN 錯誤', 70: '操作 PIN 已暫時鎖定，請稍後再試',
                40: '共享帳號沒有此權限', 36: '短信發送過於頻繁，請稍後再試',
                1023: '帳號風控冷卻中，請十分鐘後重試', 100: '此期間未找到資料'}.get(code, '中國區服務拒絕請求')
        if code in AUTH_CODES: raise ChinaAuthRequired('中國區登入資料已失效', code)
        raise ChinaError(text + ('（%s）' % code if code is not None else ''), code)
    return result


class Protocol(p.LeapmotorClient):
    def __init__(self, session=None, transport=None):
        super().__init__(session)
        self.transport = transport or http_request

    def _http(self, method, url, headers, body=None, form=False):
        if float(get_setting('cn_login_cooldown', '0')) > time.time():
            raise ChinaError('帳號風控冷卻中，請稍後重試', 1023)
        result = self.transport(method, url, headers, body, form)
        try:
            return check(result, legacy=url.startswith(p.ACCOUNT_BASE))
        except ChinaError as error:
            if error.code == 1023: set_setting('cn_login_cooldown', time.time() + 600)
            raise

    def build_headers(self, payload=None):
        signed = self._signed_headers()
        combined = dict(signed, **(payload or {}))
        message = ''.join(str(v) for _, v in sorted(combined.items()))
        signature = hmac.new(self.session.hkdf_key, message.encode(), hashlib.sha256).hexdigest()
        return dict(signed, **self._extra_headers(), sign=signature, digest=signature)


class ChinaAPIClient:
    _mate_cn_api = True
    _mate_new_api = True  # bypass the retired session_share monkey patch

    def __init__(self, username='', password='', operation_password=None, device_id=None, transport=None, **kwargs):
        self.username = username
        self.operation_password = operation_password or ''
        self.device_id = device_id or secrets.token_hex(16)
        self._p = Protocol(p.Session(device_id=self.device_id, operate_pwd_mode='aes_jwt'), transport)
        self._mutex = threading.RLock()
        self._routes, self._vehicles, self._configs = {}, {}, {}
        self._vehicles_read_at = 0
        self._generation = None
        self.on_login = None
        self.last_new_command_receipt = None

    def __repr__(self): return 'ChinaAPIClient(<private>)'
    def close(self): pass

    @property
    def refresh_token(self): return self._p.session.car_refresh_token

    def _envelope(self):
        return {'market': 'CN', 'profile': PROFILE, 'revision': secrets.token_hex(16),
                'session': {key: getattr(self._p.session, key) for key in SESSION_FIELDS}}

    def _restore(self):
        value = get_setting(SESSION_KEY)
        if not value: return False
        try: blob = json.loads(crypto.decrypt(value))
        except (ValueError, TypeError): return False
        if blob.get('market') != 'CN' or blob.get('profile') != PROFILE: return False
        if self.username and blob['session'].get('phone') != self.username: return False
        if not blob['session'].get('car_token'): return False
        if blob.get('revision') != self._generation:
            self._p.session = p.Session(**{k: v for k, v in blob['session'].items() if k in SESSION_FIELDS}, operate_pwd_mode='aes_jwt')
            self._p.session.seal_key()
            self._generation = blob['revision']
            self.device_id = self._p.session.device_id
        return True

    def _save(self):
        blob = self._envelope()
        set_setting(SESSION_KEY, crypto.encrypt(json.dumps(blob)))
        self._generation = blob['revision']

    def _locked(self): return exclusive(db_path().with_suffix('.cn-session.lock'))

    def login(self):
        with self._mutex, self._locked():
            if not self._restore(): raise ChinaAuthRequired('請在設定頁以短信驗證碼登入中國區帳號')
            self._ensure_fresh()

    def _expiry(self):
        try:
            part = self._p.session.car_token.split('.')[1]
            return float(json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))['exp'])
        except (ValueError, KeyError, IndexError, TypeError): raise ChinaAuthRequired('車端登入資料格式無效') from None

    def _ensure_fresh(self):
        if time.time() + 120 >= self._expiry(): self._renew()

    def _renew(self):
        before = copy.deepcopy(self._p.session)
        try:
            try:
                response = self._signed(GLOBAL, '/base/base-user/token/v1/refresh',
                    {'refreshToken': before.car_refresh_token}, form=False)
                self._apply_gateway(response)
            except ChinaAuthRequired:
                if not before.refresh_token: raise ChinaAuthRequired('請重新以短信驗證碼登入')
                self._p.refresh_account_token()
                response = self._p.car_login(tries=1)
                self._apply_gateway(response)
            self._save()
        except Exception:
            self._p.session = before
            raise

    def _apply_gateway(self, response):
        data = check(response)['data']
        if not isinstance(data, dict) or not all(data.get(k) for k in ('accessToken', 'refreshToken', 'signParam')):
            raise ChinaAuthRequired('車端續期回應不完整，已保留原登入資料')
        self._p.session.car_token = data['accessToken']
        self._p.session.car_refresh_token = data['refreshToken']
        self._p.session.sign_param = data['signParam']
        self._p.session.hkdf_key_hex = ''
        self._p.session.seal_key()
        self._expiry()

    def token_refresh(self):
        with self._mutex, self._locked():
            if not self._restore(): raise ChinaAuthRequired('請重新以短信驗證碼登入')
            self._renew()

    def send_sms(self, phone):
        phone = phone_number(phone)
        with self._mutex, self._locked():
            if float(get_setting('cn_sms_next', '0')) > time.time(): raise ChinaError('請等待倒數結束再發送短信', 36)
            if float(get_setting('cn_login_cooldown', '0')) > time.time(): raise ChinaError('帳號風控冷卻中，請稍後重試', 1023)
            try: self._p.request_sms_code(phone)
            except ChinaError as exc:
                if exc.code == 1023: set_setting('cn_login_cooldown', time.time() + 600)
                raise
            set_setting('cn_sms_next', time.time() + 60)

    def verify_sms(self, phone, code):
        phone = phone_number(phone)
        if not re.fullmatch(r'[0-9]{4,8}', str(code)): raise ChinaError('請輸入有效的短信驗證碼')
        with self._mutex, self._locked():
            if float(get_setting('cn_login_cooldown', '0')) > time.time(): raise ChinaError('帳號風控冷卻中，請稍後重試', 1023)
            self._p.session = p.Session(device_id=self.device_id, operate_pwd_mode='aes_jwt')
            try:
                self._p.login(phone, str(code))
                if not self._p.session.token or not self._p.session.user_id:
                    raise ChinaAuthRequired('短信登入回應不完整，請重新確認驗證碼')
                self._apply_gateway(self._p.car_login(tries=1))
            except ChinaError as exc:
                if exc.code == 1023: set_setting('cn_login_cooldown', time.time() + 600)
                raise
            login_id = secrets.token_urlsafe(24)
            # Discovery uses the staged session and never performs a second login.
            cars = self._list_vehicles(persist=False)
            if not cars: raise ChinaError('此帳號未找到車輛，請確認已接受車輛共享')
            blob = self._envelope()
            blob['expires_at'] = time.time() + 900
            blob['vehicles'] = [dataclasses.asdict(car) for car in cars]
            set_setting('cn_pending_' + login_id, crypto.encrypt(json.dumps(blob)))
            return login_id, cars

    @classmethod
    def complete_setup(cls, login_id):
        with exclusive(db_path().with_suffix('.cn-session.lock')):
            key = 'cn_pending_' + str(login_id)
            try: blob = json.loads(crypto.decrypt(get_setting(key)))
            except (ValueError, TypeError): raise ChinaAuthRequired('登入流程已失效，請重新登入') from None
            if blob.get('expires_at', 0) < time.time() or blob.get('market') != 'CN': raise ChinaAuthRequired('登入流程已過期，請重新登入')
            blob.pop('expires_at', None)
            cars = blob.pop('vehicles', [])
            with connect() as db:
                for k, v in ((SESSION_KEY, crypto.encrypt(json.dumps(blob))),
                             ('leapmotor_user', blob['session']['phone']), ('cloud_market', 'CN')):
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (k, v))
                db.execute("DELETE FROM settings WHERE key LIKE 'cn_pending_%'")
                for car in cars:
                    caps = {k: car[k] for k in ('is_shared', 'abilities', 'rights', 'module_rights')}
                    caps['account'] = hashlib.sha256(blob['session']['phone'].encode()).hexdigest()
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                        ('cn_capabilities_' + car['vin'].lower(), json.dumps(caps)))
            return blob['session']['phone']

    def _signed(self, host, path, body=None, method='POST', form=True, vin=''):
        body = dict(body or {})
        headers = self._p.build_headers(body)
        headers['carvin'] = vin
        headers['cartype'] = self._vehicles[vin.lower()].car_type if vin.lower() in self._vehicles else ''
        url = origin(host) + path
        if method == 'GET':
            if body: url += '?' + urlencode(body)
            body = None
        return self._p._http(method, url, headers, body, form)

    def _read(self, host, path, body=None, method='GET', form=True, vin=''):
        with self._mutex, self._locked():
            if not self._restore(): raise ChinaAuthRequired('請在設定頁以短信驗證碼登入')
            self._ensure_fresh()
            try: return self._signed(host, path, body, method, form, vin)
            except ChinaAuthRequired:
                self._renew()
                return self._signed(host, path, body, method, form, vin)

    def _list_vehicles(self, persist=True):
        result = self._signed(GLOBAL, '/app/app-global-service/v1/vehicle/list', method='GET')
        cars = []
        for bucket, shared in (('bindcars', False), ('sharedcars', True)):
            for raw in (result.get('data') or {}).get(bucket) or []:
                car = Vehicle.from_dict(raw, shared)
                if car.vin.lower() in {v.vin.lower() for v in cars}: continue
                cars.append(car)
        self._vehicles = {v.vin.lower(): v for v in cars}
        self._vehicles_read_at = time.time()
        for car in cars if persist else []:
            caps = dict(is_shared=car.is_shared, abilities=car.abilities, rights=car.rights, module_rights=car.module_rights)
            caps['account'] = hashlib.sha256(self._p.session.phone.encode()).hexdigest()
            set_setting('cn_capabilities_' + car.vin.lower(), json.dumps(caps))
        return cars

    def get_vehicle_list(self):
        with self._mutex, self._locked():
            if not self._restore(): raise ChinaAuthRequired('請以短信驗證碼登入')
            self._ensure_fresh()
            try: return self._list_vehicles()
            except ChinaAuthRequired:
                self._renew(); return self._list_vehicles()

    def _vehicle(self, vehicle):
        vin = vehicle.vin if hasattr(vehicle, 'vin') else str(vehicle)
        if not self._vehicles or (self._vehicles_read_at and time.time() - self._vehicles_read_at > 600): self.get_vehicle_list()
        car = self._vehicles.get(vin.lower())
        if car is None: raise ChinaError('此車輛不在目前帳號內')
        return car

    def route_for(self, vehicle):
        car = self._vehicle(vehicle)
        if car.vin not in self._routes:
            result = self._read(GLOBAL, '/app/app-global-service/v1/vehicle/getCarRoute', {'vin': car.vin}, vin=car.vin)
            self._routes[car.vin] = origin(result['data']['appRegion'])
        return self._routes[car.vin]

    def get_vehicle_raw_status(self, vehicle):
        car = self._vehicle(vehicle)
        return self._read(self.route_for(car), '/app/app-signal-service/signal/info/query',
            {'vin': car.vin}, method='POST', form=False, vin=car.vin)

    def get_vehicle_snapshot(self, vehicle):
        from cn_telemetry import decode_status
        car = self._vehicle(vehicle)
        snapshot = decode_status(self.get_vehicle_raw_status(car), car)
        cached = self._configs.get(car.vin)
        if cached and cached[1].get('percent') is not None:
            snapshot.data.charge_limit_percent = cached[1]['percent']
            snapshot.reported_fields = snapshot.data.reported_fields = snapshot.reported_fields | {'charge_limit_percent'}
        return snapshot

    def _car_read(self, path, body, vehicle):
        car = self._vehicle(vehicle)
        if car.is_shared and 400 not in car.module_rights and '/drivingrecord/' in path:
            raise ChinaError('共享帳號沒有里程能耗權限', 40)
        return self._read(self.route_for(car), '/carownerservice/v3/api/' + path, body, vin=car.vin)

    def _config(self, vehicle):
        car = self._vehicle(vehicle)
        old = self._configs.get(car.vin)
        if old and time.time() - old[0] < 60: return old[1]
        result = self._car_read('vehicleinfo/commonConfig', {'vin': car.vin}, car)
        config = (result.get('data') or {}).get('config', {}).get('3') or {}
        self._configs[car.vin] = (time.time(), config)
        return config

    def get_charge_schedule(self, vin):
        cur = self._config(vin)
        mapping = {'chargeEnable': 'isEnable', 'chargesoc': 'percent', 'starttime': 'beginTime',
                   'endtime': 'endTime', 'cycles': 'cycles', 'circulation': 'circulation', 'recharge': 'recharge'}
        return {k: cur.get(v) for k, v in mapping.items()}

    def get_consumption_breakdown(self, vehicle, begin_ts, end_ts):
        result = self._car_read('drivingrecord/getLastweekEC',
            {'carvin': self._vehicle(vehicle).vin, 'begintime': int(begin_ts), 'endtime': min(int(end_ts), int(time.time()))}, vehicle)
        data = result.get('data') or {}
        return SimpleNamespace(driver_ec=float(data['driverEC']), ac_ec=float(data['acEC']), other_ec=float(data['otherEC']))

    def get_consumption_last_week_breakdown(self, vehicle):
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        end = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.get_consumption_breakdown(vehicle, int((end - timedelta(days=7)).timestamp()), int(end.timestamp()) - 1)

    def get_consumption_weekly_rank(self, vehicle):
        data = self._car_read('drivingrecord/getLastNweeks100kmECAndRank', {'carvin': self._vehicle(vehicle).vin}, vehicle)['data']
        rank = data.get('rankResult') or {}
        return SimpleNamespace(rank=SimpleNamespace(rank=rank.get('rank'), hundred_km_ec=rank.get('hundredKmEC')),
            weekly=[SimpleNamespace(week_start=w.get('weekStart'), week_end=w.get('weekEnd'), hundred_km_ec=w.get('hundredKmEC')) for w in data.get('weeklyEC') or []])

    def get_mileage(self, vehicle, begin=None, end=None):
        body = {'vin': self._vehicle(vehicle).vin}
        if begin is not None: body.update(begintime=int(begin) * 1000, endtime=min(int(end), int(time.time())) * 1000)
        return self._car_read('drivingrecord/mileage/energy/detail', body, vehicle)

    def get_car_picture(self, vehicle):
        return self._car_read('carpicture/key', {'vin': self._vehicle(vehicle).vin}, vehicle)

    def download_picture(self, url):
        """Download a CN appearance render without forwarding account credentials to the CDN."""
        from io import BytesIO
        from PIL import Image
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
            or not any(host == suffix or host.endswith('.' + suffix) for suffix in ('aliyuncs.com', 'leapmotor.com', 'leapmotor.cn'))):
            raise ChinaError('車輛圖片網址無效')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                    urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(url, timeout=20) as response: raw = response.read(20_000_001)
            if len(raw) > 20_000_000: raise ValueError()
            with Image.open(BytesIO(raw)) as image:
                if image.width * image.height > 20_000_000: raise ValueError()
                output = BytesIO(); image.convert('RGBA').save(output, format='PNG')
                return output.getvalue()
        except (OSError, ValueError): raise ChinaError('車輛圖片暫時無法讀取') from None

    def get_daily_energy(self, vehicle, start, end):
        """Day-granularity official totals. Closed days are cached, today's reading has a TTL."""
        from datetime import date, time as day_time
        car = self._vehicle(vehicle)
        if car.is_shared and 400 not in car.module_rights:
            raise ChinaError('共享帳號沒有里程能耗權限', 40)
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        start, end = date.fromisoformat(start), date.fromisoformat(end)
        if start > end or (end - start).days > 30 or end > today: raise ChinaError('請選擇截至今天、最多 31 天的日期範圍')
        rows, budget = [], 10
        day = start
        while day <= end:
            key = 'cn_energy_' + car.vin.lower() + '_' + day.isoformat()
            try: cached = json.loads(get_setting(key) or '{}')
            except ValueError: cached = {}
            valid = cached.get('data') and (day < today or time.time() - cached.get('at', 0) < 1800)
            if not valid and budget and time.time() >= cached.get('retry_at', 0):
                budget -= 1
                begin = int(datetime.combine(day, day_time.min, ZoneInfo('Asia/Shanghai')).timestamp())
                finish = min(int(datetime.combine(day + timedelta(days=1), day_time.min, ZoneInfo('Asia/Shanghai')).timestamp()) - 1, int(time.time()))
                try:
                    result = self.get_consumption_breakdown(car, begin, finish)
                    values = {'driver_kwh': result.driver_ec, 'ac_kwh': result.ac_ec, 'other_kwh': result.other_ec}
                    cached = {'at': time.time(), 'data': values}
                except ChinaError as error:
                    if error.code == 100 and day < today:
                        cached = {'at': time.time(), 'data': {'driver_kwh': 0, 'ac_kwh': 0, 'other_kwh': 0}}
                    else: cached = {**cached, 'retry_at': time.time() + 3600}
                set_setting(key, json.dumps(cached))
            values = cached.get('data') or {}
            rows.append({'day': day.isoformat(), **values, 'total_kwh': sum(values.values()) if values else None,
                         'source': 'official' if values else 'unavailable'})
            day += timedelta(days=1)
        return rows

    def _command(self, vin, action, cmd, state, path=CONTROL):
        car = self._vehicle(vin)
        allowed, reason = permission(car, action)
        if not allowed: raise UnsupportedFeature(reason)
        if not self.operation_password: raise ChinaError('請先設定此車輛的操作 PIN')
        host = self.route_for(car)
        with self._mutex, self._locked():
            if not self._restore(): raise ChinaAuthRequired('請重新以短信驗證碼登入')
            self._ensure_fresh()
            def signed_form(fields):
                fields = dict(fields, deviceID=self.device_id, timespan=str(int(time.time() * 1000)), nonce=str(secrets.randbelow(9990000) + 10000))
                values = dict(fields, token=self._p.session.car_token)
                fields['signStr'] = hashlib.md5(''.join(str(v) for _, v in sorted(values.items())).encode()).hexdigest()[8:24]
                return fields
            self._p.session.operate_pwd = self.operation_password
            pwd = self._p._encrypted_pwd()
            self._signed(host, '/carownerservice/v3/api/appoperate/verifyoperatepwdnew',
                signed_form({'vin': car.vin, 'oprpwd': pwd}), vin=car.vin)
            body = {'carvin': car.vin, 'oppwd': pwd}
            if cmd is not None: body.update(cmdid=str(cmd), state=json.dumps(state, separators=(',', ':')))
            else: body.update(state=state['state'])
            self.last_new_command_receipt = None
            response = self._signed(host, path, signed_form(body), vin=car.vin)
            data = response.get('data')
            message_id = data.get('eventId', data.get('msgID')) if isinstance(data, dict) else data
            tracked = isinstance(message_id, (str, int)) and bool(str(message_id))
            self.last_new_command_receipt = SimpleNamespace(outcome='accepted' if tracked else 'accepted_untracked',
                event_id=str(message_id) if tracked else None, api_code=0, timeout_seconds=response.get('timeout'),
                physical_execution_confirmed=False)
            # Charge plans have no live signal confirmation; require read-back or a car reply.
            if action in ('charge_limit', 'charge_schedule') and tracked:
                deadline = time.monotonic() + min(float(response.get('timeout') or 10), 30)
                while time.monotonic() < deadline:
                    result = self._signed(host, CONTROL + '/query', {'msgID': str(message_id), 'carvin': car.vin}, method='GET', vin=car.vin)
                    if type(result.get('data')) is int and result['data'] == 1:
                        self.last_new_command_receipt.outcome = 'confirmed'; break
                    time.sleep(1.5)
                self._configs.pop(car.vin, None)
            return response

    def lock_vehicle(self, vin): return self._command(vin, 'lock', 110, {'value': 'lock'})
    def unlock_vehicle(self, vin): return self._command(vin, 'unlock', 110, {'value': 'unlock'})
    def find_vehicle(self, vin): return self._command(vin, 'find_car', 120, {'value': 'true'})
    def open_trunk(self, vin): return self._command(vin, 'open_trunk', 130, {'value': 'true'})
    def close_trunk(self, vin): return self._command(vin, 'close_trunk', 130, {'value': 'false'})
    def open_sunshade(self, vin): return self._command(vin, 'open_sunshade', 240, {'value': '10'})
    def close_sunshade(self, vin): return self._command(vin, 'close_sunshade', 240, {'value': '0'})
    def windows(self, vin, value='0'):
        if str(value) not in ('0', '2', '5', '10'): raise ChinaError('車窗只支援關閉、微開、半開及全開')
        return self._command(vin, 'windows', 230, {'value': str(value)})
    def battery_preheat(self, vin): return self._command(vin, 'battery_preheat', 160, {'value': 'ptcon'})
    def battery_preheat_off(self, vin): return self._command(vin, 'battery_preheat_off', 160, {'value': 'ptcoff'})
    def steering_wheel_heat_on(self, vin): return self._command(vin, 'steering_heat', 320, {'level': '2'})
    def steering_wheel_heat_off(self, vin): return self._command(vin, 'steering_heat', 320, {'level': '1'})
    def rearview_mirror_heat_on(self, vin): return self._command(vin, 'mirror_heat', 440, {'value': '2'})
    def rearview_mirror_heat_off(self, vin): return self._command(vin, 'mirror_heat', 440, {'value': '1'})
    def set_seat_heat(self, vin, position='driver', level=0): return self._seat(vin, position, level, True)
    def set_seat_vent(self, vin, position='driver', level=0): return self._seat(vin, position, level, False)
    def _seat(self, vin, position, level, heat):
        if position not in ('driver', 'copilot') or type(level) is not int or not 0 <= level <= 3: raise ChinaError('座椅設定無效')
        return self._command(vin, 'seat_heat' if heat else 'seat_vent', 301 if heat else 370, {'position': position, 'level': str(level)})
    def ac_on(self, vin, **kwargs): return self.ac_switch(vin, **kwargs)
    def ac_off(self, vin, **kwargs):
        values = dict(temperature=26, mode='nohotcold', windlevel=4, circle='out', wshld='1')
        values.update(kwargs)
        return self.ac_switch(vin, operate='off', **values)
    def _climate_preset(self, vin, key):
        return self._command(vin, 'climate', 170, dict(p.LeapmotorClient.AC_QUICK[key]))
    def quick_cool(self, vin): return self._climate_preset(vin, 'boost_cool')
    def quick_heat(self, vin): return self._climate_preset(vin, 'boost_heat')
    def quick_vent(self, vin): return self._climate_preset(vin, 'deodorize')
    def windshield_defrost(self, vin): return self._climate_preset(vin, 'defrost')
    def seat_heat(self, vin, position='driver', level=0): return self._seat(vin, position, int(level), True)
    def seat_ventilation(self, vin, position='driver', level=0): return self._seat(vin, position, int(level), False)
    def _remote_control(self, vin, action, cmd_content):
        try: body = json.loads(cmd_content)
        except (ValueError, TypeError): raise ChinaError('空調設定格式無效') from None
        if action in ('seat_heat', 'seat_ventilation'):
            return self._seat(vin, body.get('position'), int(body.get('level')), action == 'seat_heat')
        if action == 'steering_wheel_heat': return self._command(vin, 'steering_heat', 320, body)
        if action == 'rearview_mirror_heat': return self._command(vin, 'mirror_heat', 440, body)
        if action not in ('ac_on', 'ac_off', 'ac_switch'): raise UnsupportedFeature('此中國區控制尚未支援')
        return self.ac_switch(vin, **body)
    def ac_switch(self, vin, temperature=24, mode='cold', operate='manual', windlevel=3, circle='in', **kwargs):
        params = kwargs.pop('params', None)
        if params:
            if params == {'operate': 'off'}: return self.ac_off(vin)
            merged = dict(temperature=temperature, mode=mode, operate=operate, windlevel=windlevel, circle=circle, **kwargs)
            merged.update(params)
            return self.ac_switch(vin, **merged)
        if mode not in ('cold', 'hot', 'wind', 'nohotcold') or operate not in ('manual', 'auto', 'off') or circle not in ('in', 'out'):
            raise ChinaError('空調設定無效')
        if not math.isfinite(float(temperature)) or not 18 <= float(temperature) <= 32 or not 1 <= int(windlevel) <= 7: raise ChinaError('空調溫度或風量超出範圍')
        return self._command(vin, 'climate', 170, {'circle': circle, 'mode': mode, 'operate': operate,
            'position': 'all', 'temperature': str(int(float(temperature))), 'windlevel': int(windlevel), 'wshld': str(kwargs.get('wshld', '1' if operate == 'off' else '0'))})
    def set_charge_schedule(self, vin, **values):
        allowed, reason = permission(self._vehicle(vin), 'charge_schedule')
        if not allowed: raise UnsupportedFeature(reason)
        cur = self.get_charge_schedule(vin)
        aliases = {'enabled': 'chargeEnable', 'soc_limit': 'chargesoc', 'start_time': 'starttime', 'end_time': 'endtime'}
        values = {aliases.get(k, k): v for k, v in values.items()}
        for key, value in values.items():
            if key in cur: cur[key] = value
        required = {'chargeEnable', 'chargesoc', 'starttime', 'endtime', 'cycles', 'circulation', 'recharge'}
        if any(cur.get(k) is None for k in required): raise ChinaError('充電計畫資料不完整，未覆寫設定')
        for key in ('chargeEnable', 'chargesoc', 'circulation', 'recharge'): cur[key] = int(cur[key])
        if not 50 <= cur['chargesoc'] <= 100 or cur['chargeEnable'] not in (0, 1): raise ChinaError('充電上限或啟用設定無效')
        if any(not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', str(cur[k])) for k in ('starttime', 'endtime')): raise ChinaError('充電時間格式無效')
        if not re.fullmatch(r'[01](?:,[01]){6}', str(cur['cycles'])): raise ChinaError('充電日期設定無效')
        return self._command(vin, 'charge_schedule', 190, cur)
    def set_charge_limit(self, vin, percent): return self.set_charge_schedule(vin, chargesoc=int(percent))
    def healthy_charging(self, vin, on=True): return self._command(vin, 'healthy_charging', None, {'state': int(on)}, '/carownerservice/v3/api/healthyCharging/control')

    def __getattr__(self, name):
        if name.startswith(('get_', 'set_', 'send_', 'prepare_', 'sentry_', 'start_', 'stop_', 'unlock_')):
            def unavailable(*args, **kwargs): raise UnsupportedFeature('此功能尚未支援中國區，未發送請求')
            return unavailable
        raise AttributeError(name)
