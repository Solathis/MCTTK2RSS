"""test_poster_login.py — MCBBS 登录：验证码页定位、失败重试与排查证据落盘"""
import json

import pytest

import poster
from poster import LoginRejected, LoginRetryable, MCBBSPoster

BASE = "https://www.mcbbs.co"
FORMHASH = "abc12345"
FORUM_FORMHASH = "f0f0f0f0"
CAPTCHA_FORMHASH = "c0de1234"
CAPTCHA_IDHASH = "deadbeef"
LONG_AUTH = "AbCdEf0123456789XyZw"


class FakeResponse:
    def __init__(self, text="", content=b"", status_code=200):
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise poster.requests.HTTPError(f"HTTP {self.status_code}")


class ScriptedSession:
    """按 URL 片段匹配预设响应的假会话；每条预设只消费一次，避免拿错页面。"""

    def __init__(self, routes):
        self.routes = list(routes)
        self.headers = {}
        self.requests = []
        self.calls = []

    def _answer(self, method, url, kwargs):
        self.requests.append((method, url))
        self.calls.append((method, url, kwargs))
        for index, (route_method, part, _response) in enumerate(self.routes):
            if route_method == method and part in url:
                return self.routes.pop(index)[2]
        raise AssertionError(f"未预设的请求: {method} {url}")

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)


class FakeOcr:
    def classification(self, image: bytes) -> str:
        return "AB12"


def _login_page(formhash: str = FORMHASH, auth: str = "") -> str:
    html = f'<input type="hidden" name="formhash" value="{formhash}">'
    if auth:
        html += f'<a href="member.php?mod=logging&action=login&auth={auth}&referer=x">重试</a>'
    return html


def _captcha_form(auth: str = LONG_AUTH) -> str:
    return (
        _login_page(CAPTCHA_FORMHASH, auth)
        + "<label>验证码</label>"
        + f"<script type='text/javascript'>updateseccode('{CAPTCHA_IDHASH}', 'member::logging')</script>"
    )


def _forum_page() -> str:
    return (
        "<script>discuz_uid = '42';</script>"
        f'<input type="hidden" name="formhash" value="{FORUM_FORMHASH}">'
    )


def _make_poster(max_attempts: int = 3, wait_seconds: int = 0) -> MCBBSPoster:
    return MCBBSPoster({
        "base_url": BASE,
        "forum_fid": 45,
        "username": "user",
        "password": "secret",
        "login_retry": {"max_attempts": max_attempts, "wait_seconds": wait_seconds},
    })


@pytest.fixture(autouse=True)
def _isolated_login_env(monkeypatch, tmp_path):
    """登录失败的证据会落盘、验证码会写 captcha.png，全部挪到临时目录。"""
    logs = tmp_path / "logs"
    monkeypatch.setattr(poster, "_login_debug_dir", lambda: str(logs))
    monkeypatch.setattr(poster.time, "sleep", lambda *_: None)
    monkeypatch.chdir(tmp_path)
    return logs


class TestLoginRetry:
    def test_retries_transient_failure_with_new_session(self, monkeypatch):
        sessions = [
            ScriptedSession([("GET", "action=login", FakeResponse("<html>请稍后访问</html>"))]),
            ScriptedSession([
                ("GET", "action=login", FakeResponse(_login_page())),
                ("POST", "loginsubmit=yes", FakeResponse("欢迎您回来")),
                ("GET", "forum.php", FakeResponse(_forum_page())),
            ]),
        ]
        monkeypatch.setattr(poster, "_make_session", lambda: sessions.pop(0))

        p = _make_poster(max_attempts=2)
        assert p.login() is True
        assert p.formhash == FORUM_FORMHASH

    def test_gives_up_after_max_attempts(self, monkeypatch):
        sessions = [
            ScriptedSession([("GET", "action=login", FakeResponse("<html>请稍后访问</html>"))]),
            ScriptedSession([("GET", "action=login", FakeResponse("<html>请稍后访问</html>"))]),
        ]
        monkeypatch.setattr(poster, "_make_session", lambda: sessions.pop(0))

        p = _make_poster(max_attempts=2)
        with pytest.raises(LoginRetryable, match="formhash"):
            p.login()
        assert sessions == []  # 两次尝试都用了新会话

    def test_does_not_retry_rejected_credentials(self, monkeypatch):
        session = ScriptedSession([
            ("GET", "action=login", FakeResponse(_login_page())),
            ("POST", "loginsubmit=yes", FakeResponse('<div id="messagetext">密码错误</div>')),
        ])
        monkeypatch.setattr(poster, "_make_session", lambda: session)

        p = _make_poster(max_attempts=3)
        with pytest.raises(LoginRejected, match="密码错误"):
            p.login()
        assert [method for method, _ in session.requests] == ["GET", "POST"]

    def test_retries_transient_login_message(self, monkeypatch):
        sessions = [
            ScriptedSession([
                ("GET", "action=login", FakeResponse(_login_page())),
                ("POST", "loginsubmit=yes",
                 FakeResponse('<div id="messagetext">抱歉，您的请求来路不正确或表单验证串不符</div>')),
            ]),
            ScriptedSession([
                ("GET", "action=login", FakeResponse(_login_page())),
                ("POST", "loginsubmit=yes", FakeResponse("欢迎您回来")),
                ("GET", "forum.php", FakeResponse(_forum_page())),
            ]),
        ]
        monkeypatch.setattr(poster, "_make_session", lambda: sessions.pop(0))

        assert _make_poster(max_attempts=2).login() is True
        assert sessions == []

    def test_login_requests_have_timeout(self, monkeypatch):
        session = ScriptedSession([
            ("GET", "action=login", FakeResponse(_login_page())),
            ("POST", "loginsubmit=yes", FakeResponse("欢迎您回来")),
            ("GET", "forum.php", FakeResponse(_forum_page())),
        ])
        monkeypatch.setattr(poster, "_make_session", lambda: session)

        assert _make_poster().login() is True
        assert session.calls
        assert all(kwargs.get("timeout") == poster.LOGIN_TIMEOUT for _, _, kwargs in session.calls)


class TestCaptchaLogin:
    def test_uses_hash_from_failed_login_response(self, monkeypatch):
        monkeypatch.setattr(poster, "_init_ocr", lambda: FakeOcr())
        failed = FakeResponse(_captcha_form())
        session = ScriptedSession([
            ("GET", "mod=seccode", FakeResponse(content=b"\x89PNG" + b"0" * 200)),
            ("POST", "loginsubmit=yes", FakeResponse("欢迎您回来")),
            ("GET", "forum.php", FakeResponse(_forum_page())),
        ])

        formhash = poster._login_with_captcha(session, BASE, "user", "secret", "", failed)

        assert formhash == FORUM_FORMHASH
        # 验证码 hash 直接取自失败的登录响应，没有再去 GET 登录页
        assert not [url for method, url in session.requests
                    if method == "GET" and "action=login" in url]
        image_urls = [url for method, url, _ in session.calls
                      if method == "GET" and "mod=seccode" in url]
        assert image_urls and f"idhash={CAPTCHA_IDHASH}" in image_urls[0]
        assert all(kwargs.get("timeout") == poster.LOGIN_TIMEOUT for _, _, kwargs in session.calls)

    def test_captcha_rejection_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(poster, "_init_ocr", lambda: FakeOcr())
        session = ScriptedSession([
            ("GET", "action=login", FakeResponse(_login_page())),
            ("POST", "loginsubmit=yes", FakeResponse(_captcha_form())),
            ("GET", "mod=seccode", FakeResponse(content=b"\x89PNG" + b"0" * 200)),
            ("POST", "loginsubmit=yes", FakeResponse('<div id="messagetext">密码错误</div>')),
        ])
        clients = []

        def make_session():
            clients.append(session)
            return session

        monkeypatch.setattr(poster, "_make_session", make_session)

        with pytest.raises(LoginRejected, match="密码错误"):
            _make_poster(max_attempts=3).login()
        assert len(clients) == 1, "凭证被拒不能换会话重试"

    def test_missing_hash_dumps_evidence_and_is_retryable(self, _isolated_login_env):
        failed = FakeResponse(_login_page(auth=LONG_AUTH))
        session = ScriptedSession([
            ("GET", "action=login", FakeResponse(_login_page(auth=LONG_AUTH))),
            ("GET", "action=login", FakeResponse(_login_page(auth=LONG_AUTH))),
            ("GET", "action=login", FakeResponse(_login_page(auth=LONG_AUTH))),
        ])

        with pytest.raises(LoginRetryable, match="验证码 hash"):
            poster._login_with_captcha(session, BASE, "user", "secret", "", failed)

        dumps = list(_isolated_login_env.glob("mcbbs_login_*.html"))
        assert dumps, "取不到验证码 hash 时必须把页面落盘，否则没法排查站点改版"
        dumped = dumps[0].read_text(encoding="utf-8")
        assert FORMHASH in dumped, "落盘的是页面本身，不能只剩打码标记"
        assert "***" in dumped, "落盘前必须抹掉 auth 令牌"
        assert LONG_AUTH not in dumped


class TestEvidenceRedaction:
    def test_scrubs_urlsafe_and_padded_auth_tokens(self):
        html = (
            '<a href="?auth=abc-DEF_123%2B==&x=1">a</a>'
            '<a href="?auth=bbb-ccc_ddd=efg&y=2">b</a>'
        )

        scrubbed = poster._scrub_login_html(html)

        assert "abc-DEF_123" not in scrubbed
        assert "bbb-ccc_ddd" not in scrubbed
        assert scrubbed.count("auth=***") == 2


class TestLoginRetryConfig:
    def test_reads_login_retry_from_config(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "mcbbs": {"username": "user", "password": "secret"},
            "retry": {"login": {"max_attempts": 5, "wait_seconds": 0}},
        }), encoding="utf-8")

        cfg = poster.load_poster_config(str(path))

        assert cfg["login_retry"] == {"max_attempts": 5, "wait_seconds": 0}
        assert MCBBSPoster(cfg)._login_retry_policy() == (5, 0)

    def test_defaults_when_config_missing(self, tmp_path):
        cfg = poster.load_poster_config(str(tmp_path / "missing.json"))

        assert cfg["login_retry"] == poster.DEFAULT_LOGIN_RETRY
        assert MCBBSPoster(cfg)._login_retry_policy() == (3, 10)

    def test_clamps_invalid_values(self):
        p = _make_poster()
        p.login_retry = {"max_attempts": 0, "wait_seconds": "x"}

        assert p._login_retry_policy() == (1, 10)
