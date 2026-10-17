"""Feedback 抓取的 TLS / 证书配置测试。

背景：curl_cffi 自带 certifi 信任库、不走系统证书存储。
代理、VPN 或杀毒软件对 feedback.minecraft.net 做 TLS 中间人解密时，
根证书不在 certifi 里，请求会以 ``curl: (60) unable to get local issuer certificate``
失败，而 ``http.verify_ssl: false`` 之前只作用于 requests 调用，对 curl_cffi 无效。
"""

import pytest

import scraper


def _cfg(**http_overrides):
    http = {
        "verify_ssl": False,
        "proxies": {},
        "user_agent": "ua",
        "accept": "*/*",
    }
    http.update(http_overrides)
    return {
        "feedback_site": {
            "base_url": "https://feedback.fake-host.test",
            "timeout": 5,
            "knowledge_base_url": "https://feedback.fake-host.test/kb",
        },
        "http": http,
    }


class FakeResponse:
    def __init__(self, status_code=200, text="<html><body>ok</body></html>"):
        self.status_code = status_code
        self.headers = {"content-type": "text/html"}
        self.text = text
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeSession:
    """按第 n 次调用返回行为的假 session（n 从 1 开始）。"""

    def __init__(self, behavior=None):
        self.calls = []
        self._behavior = behavior or (lambda n: FakeResponse())

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        result = self._behavior(len(self.calls))
        if isinstance(result, Exception):
            raise result
        return result


class FakeCurlCffi:
    def __init__(self, session):
        self.Session = lambda: session


@pytest.fixture
def logs(monkeypatch):
    messages = []

    def record(name):
        def _log(msg, **kwargs):
            messages.append((name, msg))

        return _log

    for name in ("log_error", "log_warning", "log_info", "log_debug"):
        monkeypatch.setattr(scraper, name, record(name))
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    monkeypatch.setattr(scraper, "CURL_CFFI_AVAILABLE", True)
    return messages


def _install(monkeypatch, session):
    monkeypatch.setattr(scraper, "cffi_requests", FakeCurlCffi(session))
    return session


CERT_ERROR = RuntimeError(
    "Failed to perform, curl: (60) SSL certificate problem: "
    "unable to get local issuer certificate. See https://curl.se/libcurl/c/libcurl-errors.html"
)


class TestVerifySetting:
    def test_verify_ssl_false_disables_verification(self, monkeypatch, logs):
        session = _install(monkeypatch, FakeSession())

        soup = scraper.FeedbackScraper(_cfg(verify_ssl=False)).fetch_page("/kb")

        assert soup is not None
        assert session.calls[0]["verify"] is False

    def test_verify_ssl_true_keeps_verification(self, monkeypatch, logs):
        session = _install(monkeypatch, FakeSession())

        scraper.FeedbackScraper(_cfg(verify_ssl=True)).fetch_page("/kb")

        assert session.calls[0]["verify"] is True

    def test_ca_bundle_takes_precedence(self, monkeypatch, logs):
        session = _install(monkeypatch, FakeSession())

        scraper.FeedbackScraper(
            _cfg(verify_ssl=True, ca_bundle="C:/certs/corp-ca.pem")
        ).fetch_page("/kb")

        assert session.calls[0]["verify"] == "C:/certs/corp-ca.pem"


class TestCertErrorHints:
    def test_cert_error_mentions_verify_ssl_and_ca_bundle(self, monkeypatch, logs):
        _install(monkeypatch, FakeSession(lambda n: CERT_ERROR))

        result = scraper.FeedbackScraper(_cfg(verify_ssl=True)).fetch_page("/kb")

        assert result is None
        errors = " ".join(msg for name, msg in logs if name == "log_error")
        assert "curl: (60)" in errors
        assert "verify_ssl" in errors
        assert "ca_bundle" in errors
        # 仍按 max_retries 重试，不因证书错误立刻放弃
        assert len([1 for name, _ in logs if name == "log_warning"]) == 2

    def test_cert_error_when_already_unverified_hints_network(self, monkeypatch, logs):
        _install(monkeypatch, FakeSession(lambda n: CERT_ERROR))

        scraper.FeedbackScraper(_cfg(verify_ssl=False)).fetch_page("/kb")

        errors = " ".join(msg for name, msg in logs if name == "log_error")
        assert "已跳过证书校验" in errors
        assert "代理" in errors
        # 已经关掉校验时不应再建议关校验
        assert '设置 "verify_ssl": false' not in errors

    def test_plain_network_error_has_no_tls_hint(self, monkeypatch, logs):
        _install(monkeypatch, FakeSession(lambda n: RuntimeError("Failed to perform, curl: (7) Couldn't connect")))

        scraper.FeedbackScraper(_cfg(verify_ssl=True)).fetch_page("/kb")

        errors = " ".join(msg for name, msg in logs if name == "log_error")
        assert "获取页面失败" in errors
        assert "证书" not in errors

    def test_retries_then_gives_up(self, monkeypatch, logs):
        session = _install(monkeypatch, FakeSession(lambda n: CERT_ERROR))

        result = scraper.FeedbackScraper(_cfg(verify_ssl=True)).fetch_page("/kb", max_retries=3)

        assert result is None
        assert len(session.calls) == 3

    def test_recovers_when_verification_disabled(self, monkeypatch, logs):
        # 关掉校验后同一个请求能成功，模拟用户改 config 之后的正常路径
        session = _install(monkeypatch, FakeSession(lambda n: FakeResponse()))

        soup = scraper.FeedbackScraper(_cfg(verify_ssl=False)).fetch_page("/kb")

        assert soup is not None
        assert session.calls[0]["verify"] is False
