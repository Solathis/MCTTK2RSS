"""test_bili_poster.py — B站专栏发帖模块

全部用假会话，不联网、不真投稿；断言行为（请求了什么、返回了什么），
不断言实现细节（不检查内部变量名或日志措辞的精确文本）。
"""
import json

import pytest

import bili_poster
from bili_poster import (
    BilibiliPoster,
    BiliError,
    _assert_public_url,
    _sniff_image,
    _truncate_title,
    load_bili_config,
    load_cookies,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


# ── 测试替身 ─────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload=None, status=200, raw_text=None, content=b""):
        self.status_code = status
        self._payload = payload
        self._raw_text = raw_text
        self.content = content

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
        self.headers = {}
        self.cookies = {}
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

    def urls(self, method):
        return [call[1] for call in self.calls if call[0] == method]


@pytest.fixture
def cookies(monkeypatch):
    monkeypatch.setenv("BILI_SESSDATA", "sess-data")
    monkeypatch.setenv("BILI_JCT", "csrf-token")
    return {"SESSDATA": "sess-data", "bili_jct": "csrf-token"}


def make_poster(session, **overrides):
    config = {"cookie_file": "", "submit": "draft"}
    config.update(overrides)
    return BilibiliPoster(config, {"verify_ssl": True, "timeout": 5}, session=session)


def ok(payload=None):
    return FakeResponse({"code": 0, "message": "0", "ttl": 1, "data": payload or {}})


def err(code, message="失败"):
    return FakeResponse({"code": code, "message": message, "ttl": 1})


DRAFT_OK = ok({"aid": 74916})
SUBMIT_OK = ok({"aid": 39633826, "state": 0})


# ── cookie 与配置 ────────────────────────────────────

class TestCookies:
    def test_env_overrides_cookie_file(self, tmp_path, monkeypatch):
        cookie_file = tmp_path / "cookies.json"
        cookie_file.write_text(json.dumps({"SESSDATA": "from-file", "bili_jct": "file-csrf"}), encoding="utf-8")
        monkeypatch.setenv("BILI_SESSDATA", "from-env")

        loaded = load_cookies({"cookie_file": str(cookie_file)})

        assert loaded["SESSDATA"] == "from-env"
        assert loaded["bili_jct"] == "file-csrf"

    def test_missing_file_only_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BILI_JCT", raising=False)
        monkeypatch.setenv("BILI_SESSDATA", "from-env")

        loaded = load_cookies({"cookie_file": str(tmp_path / "nope.json")})

        assert loaded == {"SESSDATA": "from-env"}

    def test_broken_file_does_not_raise(self, tmp_path):
        broken = tmp_path / "cookies.json"
        broken.write_text("{not json", encoding="utf-8")
        assert load_cookies({"cookie_file": str(broken)}) == {}

    def test_load_config_uses_defaults_when_missing(self, tmp_path):
        bili_cfg, http_cfg = load_bili_config(str(tmp_path / "config.json"))
        assert bili_cfg["submit"] == "draft"
        assert bili_cfg["enabled"] is False
        assert http_cfg == {}

    def test_load_config_merges_sections(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "bilibili": {"enabled": True, "category_id": 4},
            "http": {"timeout": 30},
        }), encoding="utf-8")

        bili_cfg, http_cfg = load_bili_config(str(path))

        assert bili_cfg["enabled"] is True
        assert bili_cfg["category_id"] == 4
        assert bili_cfg["max_inline_images"] == 20  # 默认值仍在
        assert http_cfg == {"timeout": 30}


# ── 登录 ─────────────────────────────────────────────

class TestCheckLogin:
    def test_missing_cookies(self, monkeypatch):
        monkeypatch.delenv("BILI_SESSDATA", raising=False)
        monkeypatch.delenv("BILI_JCT", raising=False)
        poster = make_poster(FakeSession())

        with pytest.raises(BiliError) as excinfo:
            poster.check_login()

        assert "SESSDATA" in str(excinfo.value)
        assert "bili_jct" in str(excinfo.value)

    def test_not_logged_in(self, cookies):
        session = FakeSession(ok({"isLogin": False}))
        with pytest.raises(BiliError):
            make_poster(session).check_login()

    def test_success_records_account(self, cookies):
        session = FakeSession(ok({"isLogin": True, "mid": 10000000, "uname": "tester"}))

        poster = make_poster(session)
        poster.check_login()

        assert poster.mid == 10000000
        assert poster.uname == "tester"
        assert session.urls("GET") == [bili_poster.NAV_URL]
        assert session.calls[0][2]["timeout"] == 5


# ── 草稿 ─────────────────────────────────────────────

class TestSaveDraft:
    def test_payload_fields(self, cookies):
        session = FakeSession(DRAFT_OK)
        poster = make_poster(session, tags=["Minecraft", "我的世界"], reprint=False, original=False)

        draft = poster.save_draft("标题", "<p>正文</p>", summary="摘要")

        assert draft["aid"] == 74916
        payload = session.calls[0][2]["data"]
        assert payload["csrf"] == "csrf-token"
        assert payload["title"] == "标题"
        assert payload["content"] == "<p>正文</p>"
        assert payload["summary"] == "摘要"
        assert payload["tags"] == "Minecraft,我的世界"
        assert payload["reprint"] == "0"
        assert payload["original"] == "0"
        assert payload["list_id"] == "0"
        # 分区只由 category 决定；tid 是卡片模板 id，不能把分区塞进去
        assert payload["category"] == "26"
        assert payload["tid"] == "1"
        assert payload["tid"] != payload["category"]

    def test_business_error_raises_with_code(self, cookies):
        session = FakeSession(err(-400, "参数错误"))
        with pytest.raises(BiliError) as excinfo:
            make_poster(session).save_draft("标题", "<p>x</p>")
        assert "-400" in str(excinfo.value)

    def test_missing_aid_raises(self, cookies):
        session = FakeSession(ok({}))
        with pytest.raises(BiliError):
            make_poster(session).save_draft("标题", "<p>x</p>")

    def test_category_fallback_on_no_permission(self, cookies):
        session = FakeSession(err(-17, "没有权限"), ok({"aid": 42}))

        draft = make_poster(session, category_id=99, candidate_categories=[4, 15]).save_draft("标题", "<p>x</p>")

        assert draft["category_id"] == 4
        assert len(session.calls) == 2
        assert session.calls[1][2]["data"]["category"] == "4"

    def test_other_error_does_not_fallback(self, cookies):
        session = FakeSession(err(-400, "参数错误"))
        with pytest.raises(BiliError):
            make_poster(session, category_id=99, candidate_categories=[4, 15]).save_draft("标题", "<p>x</p>")
        assert len(session.calls) == 1


# ── 投稿 ─────────────────────────────────────────────

class TestPublish:
    def test_draft_mode_does_not_submit(self, cookies):
        session = FakeSession(DRAFT_OK)

        result = make_poster(session).publish("标题", "<p>正文</p>")

        assert result["ok"] is True
        assert result["draft_aid"] == 74916
        assert result["url"] is None
        assert "aid=74916" in result["edit_url"]
        assert session.urls("POST") == [bili_poster.DRAFT_URL]

    def test_publish_pending_review(self, cookies):
        session = FakeSession(DRAFT_OK, ok({"aid": 39633826, "state": -2}))

        result = make_poster(session, submit="publish").publish("标题", "<p>正文</p>")

        assert result["ok"] is True
        assert result["pending_review"] is True
        assert result["state_desc"] == "待审核"
        assert result["cv_id"] == 39633826
        # 待审核时 URL 打不开，不能当已发布给出
        assert result["url"] is None

    def test_publish_live(self, cookies):
        session = FakeSession(DRAFT_OK, SUBMIT_OK)

        result = make_poster(session, submit="publish").publish("标题", "<p>正文</p>")

        assert result["ok"] is True
        assert result["pending_review"] is False
        assert result["url"] == "https://www.bilibili.com/read/cv39633826"
        assert session.calls[1][2]["data"]["aid"] == "74916"

    def test_publish_rejected(self, cookies):
        session = FakeSession(DRAFT_OK, ok({"aid": 39633826, "state": -1}))

        result = make_poster(session, submit="publish").publish("标题", "<p>正文</p>")

        assert result["ok"] is False
        assert "未通过" in result["error"]

    def test_submit_412_keeps_draft(self, cookies):
        session = FakeSession(DRAFT_OK, FakeResponse(raw_text="blocked", status=412))

        result = make_poster(session, submit="publish").publish("标题", "<p>正文</p>")

        assert result["ok"] is False
        assert result["draft_aid"] == 74916
        assert "aid=74916" in result["edit_url"]
        assert "412" in result["error"]
        assert len(session.urls("POST")) == 2  # 存草稿 + 一次提交，不重试

    def test_draft_failure_skips_submit(self, cookies):
        session = FakeSession(err(-101, "账号未登录"))

        result = make_poster(session, submit="publish").publish("标题", "<p>正文</p>")

        assert result["ok"] is False
        assert "cookie" in result["error"]
        assert session.urls("POST") == [bili_poster.DRAFT_URL]

    def test_non_json_response(self, cookies):
        session = FakeSession(FakeResponse(raw_text="<html>oops</html>"))
        with pytest.raises(BiliError):
            make_poster(session).save_draft("标题", "<p>正文</p>")


# ── 图片 ─────────────────────────────────────────────

class TestImages:
    def test_upload_image_uses_sniffed_type(self, cookies):
        session = FakeSession(ok({"url": "https://i0.hdslb.com/a.png"}))

        url = make_poster(session).upload_image(PNG_BYTES, filename="cover")

        assert url == "https://i0.hdslb.com/a.png"
        files = session.calls[0][2]["files"]
        assert files["binary"][0] == "cover.png"
        assert files["binary"][2] == "image/png"
        assert session.calls[0][2]["data"] == {"csrf": "csrf-token"}

    def test_webp_without_pillow_raises(self, cookies, monkeypatch):
        monkeypatch.setattr(bili_poster, "_webp_to_png", lambda data: None)
        session = FakeSession()

        with pytest.raises(BiliError) as excinfo:
            make_poster(session).upload_image(b"RIFF0000WEBPVP8 ", filename="x")

        assert "webp" in str(excinfo.value)
        assert session.calls == []

    def test_too_large_image_rejected(self, cookies, monkeypatch):
        monkeypatch.setattr(bili_poster, "MAX_IMAGE_BYTES", 8)
        with pytest.raises(BiliError):
            make_poster(FakeSession()).upload_image(PNG_BYTES)

    def test_download_rejects_redirect(self, cookies):
        session = FakeSession(FakeResponse(status=302, raw_text=""))
        with pytest.raises(BiliError):
            make_poster(session).download_image("https://example.com/a.png")
        assert session.calls[0][2]["allow_redirects"] is False

    def test_rehost_replaces_external_images(self, cookies):
        session = FakeSession(
            FakeResponse(status=200, content=PNG_BYTES),
            ok({"url": "https://i0.hdslb.com/new.png"}),
        )
        html = '<p>x</p><figure><img src="https://example.com/old.png" alt="a"></figure>'

        new_html, uploaded, dropped = make_poster(session).rehost_images(html)

        assert uploaded == 1
        assert dropped == 0
        assert "https://i0.hdslb.com/new.png" in new_html
        assert "example.com" not in new_html

    def test_rehost_keeps_bilibili_hosts(self, cookies):
        session = FakeSession()
        html = '<img src="https://i0.hdslb.com/a.png">'

        new_html, uploaded, dropped = make_poster(session).rehost_images(html)

        assert (uploaded, dropped) == (0, 0)
        assert html == new_html
        assert session.calls == []

    def test_rehost_drops_image_when_upload_fails(self, cookies):
        session = FakeSession(
            FakeResponse(status=200, content=PNG_BYTES),
            err(-500, "服务器错误"),
        )
        html = '<figure><img src="https://example.com/a.png"></figure>'

        new_html, uploaded, dropped = make_poster(session).rehost_images(html)

        assert (uploaded, dropped) == (0, 1)
        assert "example.com" not in new_html  # 宁可少图也不能留外链

    def test_rehost_drops_insecure_url(self, cookies):
        session = FakeSession()
        html = '<img src="http://example.com/a.png">'

        new_html, _, dropped = make_poster(session).rehost_images(html)

        assert dropped == 1
        assert "http://example.com" not in new_html
        assert session.calls == []

    def test_rehost_respects_inline_limit(self, cookies):
        session = FakeSession(
            FakeResponse(status=200, content=PNG_BYTES),
            ok({"url": "https://i0.hdslb.com/one.png"}),
        )
        html = '<img src="https://example.com/1.png"><img src="https://example.com/2.png">'

        _, uploaded, dropped = make_poster(session, max_inline_images=1).rehost_images(html)

        assert (uploaded, dropped) == (1, 1)

    def test_rehost_disabled_keeps_html(self, cookies):
        html = '<img src="https://example.com/a.png">'
        new_html, uploaded, dropped = make_poster(FakeSession(), rehost_images=False).rehost_images(html)
        assert (uploaded, dropped) == (0, 0)
        assert new_html == html


class TestPublicUrlGuard:
    def test_rejects_http(self):
        with pytest.raises(ValueError):
            _assert_public_url("http://example.com/a.png")

    def test_rejects_private_ip(self):
        with pytest.raises(ValueError):
            _assert_public_url("https://192.168.1.10/a.png")
        with pytest.raises(ValueError):
            _assert_public_url("https://127.0.0.1/a.png")

    def test_allows_public_https(self):
        _assert_public_url("https://www.minecraft.net/a.png")


# ── 文件级入口 ───────────────────────────────────────

def _write_article(tmp_path, *, title="Test Title", translated="测试标题"):
    meta = {
        "title": title,
        "translated_title": translated,
        "blocks": [
            {"type": "p", "source_text": "Hello", "translated_text": "你好世界", "meta": {}},
            {"type": "img", "source_text": "", "translated_text": "", "meta": {"src": "https://x/a.png"}},
        ],
    }
    json_path = tmp_path / "news_x.json"
    json_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "news_x.html"
    html_path.write_text("<h1>测试标题</h1>\n<p>你好世界</p>", encoding="utf-8")
    return json_path, html_path


class TestPostNewsFile:
    def test_title_suffix_summary_and_notice(self, cookies, tmp_path):
        json_path, html_path = _write_article(tmp_path)
        session = FakeSession(DRAFT_OK)

        result = make_poster(session, ai_notice="本文由 AI 翻译").post_news_file(
            "news_x", str(json_path), str(html_path)
        )

        payload = session.calls[0][2]["data"]
        assert payload["title"] == "测试标题 [AI翻译]"
        assert payload["summary"] == "你好世界"  # 图片块不参与摘要
        assert "本文由 AI 翻译" in payload["content"]
        assert result["stem"] == "news_x"
        assert result["images"] == {"uploaded": 0, "dropped": 0}

    def test_title_truncated_keeping_suffix(self, cookies, tmp_path):
        json_path, html_path = _write_article(tmp_path, translated="很长的标题" * 20)
        session = FakeSession(DRAFT_OK)

        make_poster(session, max_title_chars=12).post_news_file("news_x", str(json_path), str(html_path))

        title = session.calls[0][2]["data"]["title"]
        assert len(title) == 12
        assert title.endswith("[AI翻译]")

    def test_cover_uploaded_and_attached(self, cookies, tmp_path):
        json_path, html_path = _write_article(tmp_path)
        cover = tmp_path / "news_x.jpg"
        cover.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
        session = FakeSession(
            ok({"url": "https://i0.hdslb.com/cover.jpg"}),
            DRAFT_OK,
        )

        result = make_poster(session).post_news_file("news_x", str(json_path), str(html_path), cover_path=str(cover))

        draft_payload = session.calls[1][2]["data"]
        assert draft_payload["image_urls"] == "https://i0.hdslb.com/cover.jpg"
        assert result["cover_url"] == "https://i0.hdslb.com/cover.jpg"

    def test_cover_failure_still_publishes(self, cookies, tmp_path):
        json_path, html_path = _write_article(tmp_path)
        cover = tmp_path / "news_x.jpg"
        cover.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
        session = FakeSession(err(-500, "服务器错误"), DRAFT_OK)

        result = make_poster(session).post_news_file("news_x", str(json_path), str(html_path), cover_path=str(cover))

        assert result["ok"] is True
        assert result["cover_url"] == ""
        assert "image_urls" not in session.calls[1][2]["data"]

    def test_empty_html_raises(self, cookies, tmp_path):
        json_path, html_path = _write_article(tmp_path)
        html_path.write_text("   ", encoding="utf-8")

        with pytest.raises(BiliError):
            make_poster(FakeSession()).post_news_file("news_x", str(json_path), str(html_path))


# ── 纯函数 ───────────────────────────────────────────

class TestHelpers:
    def test_sniff_image(self):
        assert _sniff_image(PNG_BYTES)[0] == "png"
        assert _sniff_image(b"\xff\xd8\xff")[0] == "jpg"
        assert _sniff_image(b"RIFF0000WEBPVP8 ")[0] == "webp"

    def test_truncate_title_keeps_suffix(self):
        assert _truncate_title("标题", " [AI翻译]", 80) == "标题 [AI翻译]"
        assert len(_truncate_title("标" * 100, " [AI翻译]", 20)) == 20

    def test_summary_skips_images(self):
        data = {"blocks": [
            {"type": "p", "translated_text": "第一段"},
            {"type": "img", "meta": {"src": "x"}},
            {"type": "p", "translated_text": "第二段"},
        ]}
        assert BilibiliPoster._build_summary(data, 100) == "第一段 第二段"
        assert BilibiliPoster._build_summary(data, 0) == ""
