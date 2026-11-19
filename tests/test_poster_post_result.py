"""test_poster_post_result.py — 发帖响应里的帖子 ID 与审核标记"""
import json

import pytest

import poster
from poster import MCBBSPoster

BASE = "https://www.mcbbs.co"
FORMHASH = "abc12345"


class FakeResponse:
    def __init__(self, text="", status_code=200, url="", headers=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self.content = b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise poster.requests.HTTPError(f"HTTP {self.status_code}")


class ScriptedSession:
    """按 URL 片段匹配预设响应的假会话；每条预设只消费一次。"""

    def __init__(self, routes):
        self.routes = list(routes)
        self.headers = {}
        self.calls = []

    def _answer(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for index, (route_method, part, _response) in enumerate(self.routes):
            if route_method == method and part in url:
                return self.routes.pop(index)[2]
        raise AssertionError(f"未预设的请求: {method} {url}")

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)


def _newthread_page() -> str:
    return (
        f'<input type="hidden" name="formhash" value="{FORMHASH}">'
        "<script>discuz_uid = '182761';</script>"
    )


def _make_poster(response) -> MCBBSPoster:
    session = ScriptedSession([
        ("GET", "action=newthread", FakeResponse(_newthread_page())),
        ("POST", "topicsubmit=yes", response),
    ])
    poster_obj = MCBBSPoster({
        "base_url": BASE, "forum_fid": 45, "username": "newspaper", "password": "secret",
    })
    poster_obj.session = session
    poster_obj.formhash = FORMHASH
    return poster_obj


def _post(response):
    return _make_poster(response).post_thread("标题", "正文")


class TestTidExtraction:
    def test_static_url_redirect(self):
        response = FakeResponse(
            "<script type=\"text/javascript\">window.location.href='thread-6816-1-1.html';</script>"
        )
        result = _post(response)
        assert result.tid == 6816
        assert result.url == f"{BASE}/thread-6816-1-1.html"
        assert result.needs_audit is False

    def test_escaped_redirect_inside_dialog(self):
        """弹窗脚本里的引号是转义的，旧正则因此永远匹配不到帖子 ID。"""
        html = (
            "<script>showDialog('提示', 'right', null, function () { "
            "window.location.href =\\'thread-7000-1-1.html\\'; });</script>"
        )
        assert _post(FakeResponse(html)).tid == 7000

    def test_query_redirect_with_html_entities(self):
        html = (
            "<script>setTimeout(\"window.location.href ="
            "'forum.php?mod=viewthread&amp;tid=7100&extra=page%3D1';\", 3000);</script>"
        )
        result = _post(FakeResponse(html))
        assert result.tid == 7100
        assert result.url == f"{BASE}/thread-7100-1-1.html"

    def test_location_header_only(self):
        response = FakeResponse("", headers={"Location": "forum.php?mod=viewthread&tid=7200"})
        assert _post(response).tid == 7200

    def test_post_url_alone_is_not_mistaken_for_tid(self):
        """响应里没有任何跳转时不能把发帖地址里的参数当成帖子 ID。"""
        html = "<html><body>发帖成功</body></html>"
        with pytest.raises(RuntimeError, match="发帖结果不明"):
            _post(FakeResponse(html, url=f"{BASE}/forum.php?mod=post&action=newthread&fid=45"))


class TestAuditDetection:
    def test_audit_without_tid(self):
        html = (
            '<div id="messagetext" class="alert_info">'
            "<p>新主题需要审核，您的帖子通过审核后才能显示</p></div>"
        )
        result = _post(FakeResponse(html))
        assert result.tid is None
        assert result.needs_audit is True
        assert result.url == "(需审核)"

    def test_audit_keeps_tid_when_redirect_present(self):
        html = (
            "<script>window.location.href ='thread-7300-1-1.html';</script>"
            "<p>新主题需要审核，您的帖子通过审核后才能显示</p>"
        )
        result = _post(FakeResponse(html))
        assert result.tid == 7300
        assert result.needs_audit is True


class TestFailurePaths:
    def test_error_page_raises_with_message(self):
        html = (
            '<div id="messagetext" class="alert_error">'
            "<p>抱歉，您两次发表间隔少于 15 秒</p></div>"
        )
        with pytest.raises(RuntimeError, match="两次发表间隔"):
            _post(FakeResponse(html))


class TestPostNewsFile:
    def test_returns_url_string(self, tmp_path):
        """对外契约不变：post_news_file 仍然返回字符串地址。"""
        txt_path = tmp_path / "news_x.txt"
        txt_path.write_text("正文", encoding="utf-8")
        json_path = tmp_path / "news_x.json"
        json_path.write_text(json.dumps({"title": "Minecraft 26.4 快照 9"}), encoding="utf-8")

        poster_obj = _make_poster(
            FakeResponse("<script>window.location.href ='thread-7400-1-1.html';</script>")
        )
        url = poster_obj.post_news_file(
            "news_x", str(txt_path), str(json_path), str(tmp_path),
            no_image=True, attach_json=False,
        )
        assert url == f"{BASE}/thread-7400-1-1.html"
