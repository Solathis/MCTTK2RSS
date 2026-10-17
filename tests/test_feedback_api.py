"""Feedback 抓取的 API 优先链路测试。

背景：feedback.minecraft.net 的页面与 RSS 会返回 Cloudflare 挑战
（403 + ``cf-mitigated: challenge``），而 Zendesk Help Center 的公开 JSON API
可以正常访问。因此抓取改为 API 优先、页面解析兜底。
"""

import json

import pytest

import scraper

SECTION_ID = "360001186971"
SECTION_NAME = "Release Changelogs"
ARTICLE_ID = "111"
ARTICLE_URL = "https://feedback.fake-host.test/hc/en-us/articles/111-Minecraft-Bedrock"
ARTICLE_BODY = (
    "<p><strong>Posted:</strong> 25 September 2026<br><br>"
    "A new hotfix is rolling out to address issues.</p>"
    "<h2>Fixes</h2><ul><li>Fixed a crash when opening the inventory</li></ul>"
)

SECTION_JSON = {
    "articles": [
        {
            "id": int(ARTICLE_ID),
            "title": "Minecraft: Bedrock Edition 26.52 Hotfix Changelog",
            "html_url": ARTICLE_URL,
            "body": ARTICLE_BODY,
            "created_at": "2026-09-25T22:05:12Z",
            "updated_at": "2026-09-25T22:08:48Z",
            "draft": False,
        },
        {
            "id": 222,
            "title": "草稿不该出现",
            "html_url": "https://feedback.fake-host.test/hc/en-us/articles/222-draft",
            "body": "<p>draft</p>",
            "created_at": "2026-09-26T00:00:00Z",
            "draft": True,
        },
    ]
}

ARTICLE_JSON = {"article": dict(SECTION_JSON["articles"][0])}

KB_HTML = """
<html><body>
<section class="section category-section">
  <h3 class="section-tree-title">
    <a class="section-tree-title-link" href="/hc/en-us/sections/360001186971-Release-Changelogs">
      Release Changelogs
    </a>
  </h3>
  <ul class="article-list">
    <li class="article-list-item">
      <a class="article-list-link" href="/hc/en-us/articles/999-html-fallback">HTML 文章</a>
    </li>
  </ul>
</section>
</body></html>
"""


def _cfg(use_api=True, section_id=SECTION_ID, articles_count=3):
    return {
        "feedback_site": {
            "base_url": "https://feedback.fake-host.test",
            "timeout": 5,
            "use_api": use_api,
            "api_locale": "en-us",
            "knowledge_base_url": "https://feedback.fake-host.test/hc/en-us/categories/kb",
            "sections": [
                {
                    "name": SECTION_NAME,
                    "name_cn": "基岩正式版更新日志",
                    "url": "https://feedback.fake-host.test/hc/en-us/sections/360001186971-Release-Changelogs",
                    "section_id": section_id,
                    "enabled": True,
                    "articles_count": articles_count,
                }
            ],
        },
        "http": {"verify_ssl": False, "proxies": {}, "user_agent": "ua", "accept": "*/*"},
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload, ensure_ascii=False)
        self.headers = headers or {"content-type": "application/json"}
        self.encoding = "utf-8"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeSession:
    """按 URL 路由的假 session，同时记录每次请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        for matcher, response in self.routes:
            if matcher in url:
                return response() if callable(response) else response
        raise AssertionError(f"未预期的请求: {url}")


class FakeCurlCffi:
    def __init__(self, session):
        self.Session = lambda: session


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(scraper, "CURL_CFFI_AVAILABLE", True)
    monkeypatch.setattr(scraper, "_FEEDBACK_ARTICLE_CACHE", {})
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)


def _install(monkeypatch, routes):
    session = FakeSession(routes)
    monkeypatch.setattr(scraper, "cffi_requests", FakeCurlCffi(session))
    return session


def _section_route(payload=None, status=200):
    return (f"/sections/{SECTION_ID}/articles.json",
            lambda: FakeResponse(status, payload if payload is not None else SECTION_JSON))


class TestApiPath:
    def test_lists_articles_via_api(self, monkeypatch):
        session = _install(monkeypatch, [_section_route()])

        result = scraper.FeedbackScraper(_cfg()).get_latest_articles()

        assert SECTION_NAME in result
        articles = result[SECTION_NAME]["articles"]
        assert [a["title"] for a in articles] == ["Minecraft: Bedrock Edition 26.52 Hotfix Changelog"]
        assert articles[0]["url"] == ARTICLE_URL
        # 正文里的 Posted 日期被解析出来，供最终 JSON 使用
        assert articles[0]["release_date"] == "25 September 2026"
        assert session.calls[0]["url"].endswith(f"/api/v2/help_center/en-us/sections/{SECTION_ID}/articles.json")
        assert session.calls[0]["params"]["sort_by"] == "created_at"
        assert session.calls[0]["params"]["per_page"] == 3

    def test_draft_articles_are_skipped(self, monkeypatch):
        _install(monkeypatch, [_section_route()])

        result = scraper.FeedbackScraper(_cfg()).get_latest_articles()

        titles = [a["title"] for a in result[SECTION_NAME]["articles"]]
        assert "草稿不该出现" not in titles

    def test_list_body_is_cached_for_article_content(self, monkeypatch):
        session = _install(monkeypatch, [_section_route()])

        scraper.FeedbackScraper(_cfg()).get_latest_articles()
        content = scraper.FeedbackScraper(_cfg()).fetch_article_content(ARTICLE_URL)

        # 列表接口已带正文，取正文不应再发请求
        assert len(session.calls) == 1
        assert content["posted_date"] == "25 September 2026"
        assert "Fixed a crash when opening the inventory" in content["content"]
        assert "article-body" in content["content"]

    def test_fetch_article_content_uses_article_endpoint(self, monkeypatch):
        session = _install(monkeypatch, [("/articles/111.json", FakeResponse(200, ARTICLE_JSON))])

        content = scraper.FeedbackScraper(_cfg()).fetch_article_content(ARTICLE_URL)

        assert content["title"] == "Minecraft: Bedrock Edition 26.52 Hotfix Changelog"
        assert content["url"] == ARTICLE_URL
        assert session.calls[0]["url"].endswith("/api/v2/help_center/en-us/articles/111.json")

    def test_falls_back_to_created_at_when_body_has_no_posted(self, monkeypatch):
        payload = {"articles": [dict(SECTION_JSON["articles"][0], body="<p>no date here</p>")]}
        _install(monkeypatch, [_section_route(payload)])

        result = scraper.FeedbackScraper(_cfg()).get_latest_articles()

        assert result[SECTION_NAME]["articles"][0]["release_date"] == "25 September 2026"


class TestHtmlFallback:
    def test_api_403_falls_back_to_kb_page(self, monkeypatch):
        session = _install(
            monkeypatch,
            [
                _section_route(status=403),
                ("/hc/en-us/categories/kb", FakeResponse(200, text=KB_HTML)),
            ],
        )

        result = scraper.FeedbackScraper(_cfg()).get_latest_articles()

        assert SECTION_NAME in result
        assert result[SECTION_NAME]["articles"][0]["url"] == "/hc/en-us/articles/999-html-fallback"
        assert any("categories/kb" in call["url"] for call in session.calls)

    def test_use_api_false_skips_api(self, monkeypatch):
        session = _install(monkeypatch, [("/hc/en-us/categories/kb", FakeResponse(200, text=KB_HTML))])

        result = scraper.FeedbackScraper(_cfg(use_api=False)).get_latest_articles()

        assert SECTION_NAME in result
        assert all("/api/v2/" not in call["url"] for call in session.calls)

    def test_section_without_section_id_falls_back(self, monkeypatch):
        _install(monkeypatch, [("/hc/en-us/categories/kb", FakeResponse(200, text=KB_HTML))])

        result = scraper.FeedbackScraper(_cfg(section_id=None)).get_latest_articles()

        assert SECTION_NAME in result


class TestRateLimit:
    def test_429_waits_for_retry_after(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))
        responses = [
            FakeResponse(429, {}, headers={"retry-after": "3"}),
            FakeResponse(200, SECTION_JSON),
        ]
        _install(monkeypatch, [(_section_route()[0], lambda: responses.pop(0))])

        result = scraper.FeedbackScraper(_cfg()).get_latest_articles()

        assert sleeps == [3]
        assert SECTION_NAME in result

    def test_429_without_retry_after_uses_backoff(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))
        responses = [FakeResponse(429, {}), FakeResponse(200, SECTION_JSON)]
        _install(monkeypatch, [(_section_route()[0], lambda: responses.pop(0))])

        result = scraper.FeedbackScraper(_cfg()).get_latest_articles()

        assert sleeps and sleeps[0] >= 1
        assert SECTION_NAME in result
