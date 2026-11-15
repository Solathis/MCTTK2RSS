"""test_bili_login.py — 扫码登录工具

全部用假会话：不联网、不真扫码；断言流程行为（请求了什么、落盘了什么）。
"""
import json
import sys

import pytest

from tools.bili_login import (
    LoginError,
    mask_secret,
    merge_cookies,
    parse_cookies_from_url,
    render_qr_text,
    run_login,
    save_cookies,
    verify_login,
    wait_for_login,
)

CROSS_DOMAIN_URL = (
    "https://passport.biligame.com/crossDomain"
    "?DedeUserID=123456&DedeUserID__ckMd5=abcdef"
    "&SESSDATA=abc%2C1700000000%2Cdef*11"
    "&bili_jct=csrfvalue&gourl=https%3A%2F%2Fwww.bilibili.com"
)


class FakeResponse:
    def __init__(self, payload=None, status=200, raw_text=None, headers=None):
        self.status_code = status
        self._payload = payload
        self._raw_text = raw_text
        self.headers = headers or {}

    @property
    def text(self):
        if self._raw_text is not None:
            return self._raw_text
        return json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.cookies = {}
        self.headers = {}
        self.proxies = {}

    def _record(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.queue:
            raise AssertionError(f"没料到还会请求 {method} {url}")
        return self.queue.pop(0)

    def get(self, url, **kwargs):
        return self._record("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)


def api_ok(data):
    return FakeResponse({"code": 0, "message": "0", "ttl": 1, "data": data})


def poll_state(code, message="0"):
    return api_ok({"code": code, "message": message, "url": "", "refresh_token": "r", "timestamp": 1})


# ── 二维码渲染 ───────────────────────────────────────

class TestRenderQrText:
    def test_renders_blocks(self):
        text = render_qr_text("https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=x")
        lines = text.splitlines()
        assert "██" in text
        assert len(lines) > 20
        assert len({len(line) for line in lines}) == 1  # 每行等宽，否则二维码是歪的

    def test_missing_qrcode_dependency(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "qrcode", None)
        with pytest.raises(LoginError) as excinfo:
            render_qr_text("https://x.com")
        assert "--extra login" in str(excinfo.value)


# ── cookie 解析 ──────────────────────────────────────

class TestParseCookiesFromUrl:
    def test_keeps_percent_encoding(self):
        """SESSDATA 里的 %2C 是值的一部分，解码后 cookie 就失效了。"""
        cookies = parse_cookies_from_url(CROSS_DOMAIN_URL)
        assert cookies["SESSDATA"] == "abc%2C1700000000%2Cdef*11"
        assert cookies["bili_jct"] == "csrfvalue"
        assert cookies["DedeUserID"] == "123456"

    def test_ignores_unknown_params(self):
        cookies = parse_cookies_from_url(CROSS_DOMAIN_URL)
        assert "gourl" not in cookies

    def test_no_query(self):
        assert parse_cookies_from_url("https://example.com/") == {}
        assert parse_cookies_from_url("") == {}


class TestMergeCookies:
    def test_session_wins(self):
        merged = merge_cookies({"SESSDATA": "from-jar"}, {"SESSDATA": "from-url", "bili_jct": "csrf"})
        assert merged["SESSDATA"] == "from-jar"
        assert merged["bili_jct"] == "csrf"

    def test_filters_unknown_and_empty(self):
        merged = merge_cookies({"SESSDATA": "", "gourl": "x", "bili_jct": "csrf"}, None)
        assert merged == {"bili_jct": "csrf"}


class TestMaskSecret:
    def test_masks_most_of_value(self):
        assert mask_secret("abcdefghij") == "abcd***"
        assert mask_secret("") == "(空)"
        assert mask_secret("abc") == "***"


# ── 轮询 ─────────────────────────────────────────────

class TestWaitForLogin:
    def test_reports_states_once_then_succeeds(self):
        session = FakeSession(poll_state(86101), poll_state(86090), poll_state(0))
        seen = []

        data = wait_for_login(session, "key", timeout=10, interval=0, sleep=lambda _s: None,
                              on_status=seen.append)

        assert data["code"] == 0
        assert seen == ["等待扫码…", "已扫码，请在手机上点「确认登录」"]
        assert session.calls[0][2]["params"]["qrcode_key"] == "key"

    def test_expired_qr_raises(self):
        session = FakeSession(poll_state(86038, "二维码已失效"))
        with pytest.raises(LoginError) as excinfo:
            wait_for_login(session, "key", timeout=10, interval=0, sleep=lambda _s: None)
        assert "过期" in str(excinfo.value)

    def test_unknown_state_raises(self):
        session = FakeSession(poll_state(86083, "未知状态"))
        with pytest.raises(LoginError):
            wait_for_login(session, "key", timeout=10, interval=0, sleep=lambda _s: None)

    def test_timeout_without_request(self):
        session = FakeSession()
        with pytest.raises(LoginError) as excinfo:
            wait_for_login(session, "key", timeout=0, interval=0, sleep=lambda _s: None)
        assert "超时" in str(excinfo.value)
        assert session.calls == []


class TestVerifyLogin:
    def test_logged_in(self):
        session = FakeSession(api_ok({"isLogin": True, "mid": 10000000, "uname": "tester"}))
        data = verify_login(session)
        assert data["mid"] == 10000000

    def test_not_logged_in(self):
        session = FakeSession(api_ok({"isLogin": False}))
        with pytest.raises(LoginError):
            verify_login(session)


# ── 落盘 ─────────────────────────────────────────────

class TestSaveCookies:
    def test_writes_only_known_keys(self, tmp_path):
        path = tmp_path / "cookies.json"
        save_cookies(path, {"SESSDATA": "s", "bili_jct": "c", "gourl": "x", "empty": ""})

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload == {"SESSDATA": "s", "bili_jct": "c"}

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "cookies.json"
        save_cookies(path, {"SESSDATA": "s", "bili_jct": "c"})
        assert path.is_file()

    def test_nothing_to_save(self, tmp_path):
        with pytest.raises(LoginError):
            save_cookies(tmp_path / "cookies.json", {"gourl": "x"})


# ── 完整流程 ─────────────────────────────────────────

class TestRunLogin:
    def test_full_flow_saves_cookies(self, tmp_path):
        session = FakeSession(
            api_ok({"url": "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=key",
                    "qrcode_key": "key"}),
            poll_state(86101),
            api_ok({"code": 0, "url": CROSS_DOMAIN_URL}),
            api_ok({"isLogin": True, "mid": 123456, "uname": "tester"}),
        )
        printed = []
        out_path = tmp_path / "bili_cookies.json"

        result = run_login(
            session, out_path=out_path, timeout=10, interval=0, sleep=lambda _s: None,
            printer=printed.append,
        )

        assert result["mid"] == 123456
        assert result["uname"] == "tester"
        assert any("██" in chunk for chunk in printed), "二维码没有打印到终端"
        assert any("qrcode_key=key" in chunk for chunk in printed), "应同时给出二维码链接"

        saved = json.loads(out_path.read_text(encoding="utf-8"))
        assert saved["SESSDATA"] == "abc%2C1700000000%2Cdef*11"
        assert saved["bili_jct"] == "csrfvalue"

    def test_missing_required_cookie_raises(self, tmp_path):
        session = FakeSession(
            api_ok({"url": "https://x/scan?qrcode_key=key", "qrcode_key": "key"}),
            api_ok({"code": 0, "url": ""}),  # 成功但没有回调地址，也没有会话 cookie
        )

        with pytest.raises(LoginError) as excinfo:
            run_login(session, out_path=tmp_path / "c.json", timeout=10, interval=0,
                      sleep=lambda _s: None, printer=lambda *_a, **_k: None)

        assert "SESSDATA" in str(excinfo.value)
        assert not (tmp_path / "c.json").exists()
