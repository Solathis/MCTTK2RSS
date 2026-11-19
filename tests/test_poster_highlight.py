"""test_poster_highlight.py — 标题高亮的色板、请求参数、响应判定与发帖登记"""
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


class RecordingSession:
    """记录每次请求的假会话：高亮只看 POST 参数，登记只看有没有多余请求。"""

    def __init__(self, response, forum_page=None):
        self.response = response
        self.forum_page = forum_page or (
            f'<input type="hidden" name="formhash" value="{FORMHASH}">'
        )
        self.headers = {}
        self.posts = []
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse(self.forum_page)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


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


NEWTHREAD_PAGE = (
    f'<input type="hidden" name="formhash" value="{FORMHASH}">'
    "<script>discuz_uid = '182761';</script>"
)


def _make_poster(session, **overrides) -> MCBBSPoster:
    config = {"base_url": BASE, "forum_fid": 45, "username": "newspaper", "password": "secret"}
    config.update(overrides)
    poster_obj = MCBBSPoster(config)
    poster_obj.session = session
    poster_obj.formhash = FORMHASH
    return poster_obj


def _post_news(poster_obj, tmp_path, stem, title, message="正文"):
    txt_path = tmp_path / f"{stem}.txt"
    txt_path.write_text(message, encoding="utf-8")
    json_path = tmp_path / f"{stem}.json"
    json_path.write_text(json.dumps({"title": title}), encoding="utf-8")
    return poster_obj.post_news_file(
        stem, str(txt_path), str(json_path), str(tmp_path), no_image=True, attach_json=False,
    )


class TestColorPalette:
    def test_known_colors_map_to_discuz_index(self):
        assert poster.highlight_color_index("#EE1B2E") == 1
        assert poster.highlight_color_index("#3C9D40") == 4
        assert poster.highlight_color_index("#EC1282") == 8

    def test_case_and_padding_ignored(self):
        assert poster.highlight_color_index("  #ee1b2e ") == 1

    def test_unknown_color_returns_none(self):
        assert poster.highlight_color_index("#123456") is None
        assert poster.highlight_color_index("") is None


class TestModerationResult:
    def test_success_marker(self):
        assert poster._parse_moderation_result("<script>succeedhandle_mods('url',{},{});</script>") == (True, "")

    def test_error_message(self):
        html = (
            "<script>if(typeof errorhandle_mods=='function') "
            "{errorhandle_mods('抱歉，您没有权限执行此操作', {});}</script>"
        )
        ok, reason = poster._parse_moderation_result(html)
        assert ok is False
        assert "没有权限" in reason

    def test_unrecognized_response_reports_size(self):
        ok, reason = poster._parse_moderation_result("<html>???</html>")
        assert ok is False
        assert "未识别" in reason

    def test_plain_message_page_is_failure(self):
        html = '<div id="messagetext" class="alert_error"><p>抱歉，操作无效</p></div>'
        ok, reason = poster._parse_moderation_result(html)
        assert ok is False
        assert "操作无效" in reason


class TestHighlightThread:
    def test_payload_matches_discuz_moderate_form(self):
        session = RecordingSession(FakeResponse("<script>succeedhandle_mods('u',{},{});</script>"))
        ok, reason = _make_poster(session).highlight_thread(6808, "#3C9D40")
        assert (ok, reason) == (True, "")

        url, kwargs = session.posts[-1]
        assert "mod=topicadmin" in url and "action=moderate" in url and "inajax=1" in url
        data = kwargs["data"]
        assert data["formhash"] == FORMHASH
        assert data["fid"] == "45"
        assert data["moderate[]"] == "6808"
        assert data["operations[]"] == "highlight"
        assert data["highlight_color"] == "4"
        assert data["highlight_style[1]"] == "0"
        assert data["highlight_style[2]"] == "0"
        assert data["highlight_style[3]"] == "0"
        assert data["redirect"].endswith("/thread-6808-1-1.html")
        assert kwargs["timeout"] == poster.LOGIN_TIMEOUT

    def test_bold_sets_first_style_bit(self):
        session = RecordingSession(FakeResponse("<script>succeedhandle_mods('u',{},{});</script>"))
        _make_poster(session).highlight_thread(6808, "#EE1B2E", bold=True)
        assert session.posts[-1][1]["data"]["highlight_style[1]"] == "1"

    def test_removing_bold_keeps_color(self):
        """取消加粗 = 同色重提交 + 样式位清零，颜色不能丢。"""
        session = RecordingSession(FakeResponse("<script>succeedhandle_mods('u',{},{});</script>"))
        _make_poster(session).highlight_thread(6808, "#EE1B2E", bold=False)
        data = session.posts[-1][1]["data"]
        assert data["highlight_color"] == "1"
        assert data["highlight_style[1]"] == "0"

    def test_referer_restored_after_request(self):
        session = RecordingSession(FakeResponse("<script>succeedhandle_mods('u',{},{});</script>"))
        _make_poster(session).highlight_thread(6808, "#3C9D40")
        assert "Referer" not in session.headers

    def test_error_response_returns_reason(self):
        html = "<script>errorhandle_mods('抱歉，您没有权限执行此操作', {});</script>"
        session = RecordingSession(FakeResponse(html))
        ok, reason = _make_poster(session).highlight_thread(6808, "#3C9D40")
        assert ok is False
        assert "没有权限" in reason

    def test_unknown_color_never_hits_network(self):
        session = RecordingSession(FakeResponse("<script>succeedhandle_mods('u',{},{});</script>"))
        ok, reason = _make_poster(session).highlight_thread(6808, "#123456")
        assert ok is False
        assert "色板" in reason
        assert session.posts == []

    def test_requires_login(self):
        poster_obj = _make_poster(RecordingSession(FakeResponse("")))
        poster_obj.session = None
        with pytest.raises(RuntimeError, match="未登录"):
            poster_obj.highlight_thread(6808, "#3C9D40")

    def test_formhash_is_fetched_when_missing(self):
        session = RecordingSession(FakeResponse("<script>succeedhandle_mods('u',{},{});</script>"))
        poster_obj = _make_poster(session)
        poster_obj.formhash = None
        assert poster_obj.highlight_thread(6808, "#3C9D40")[0] is True
        assert session.gets, "缺 formhash 时应先请求首页取一份"
        assert session.posts[-1][1]["data"]["formhash"] == FORMHASH


class TestHighlightRegistration:
    def test_audited_post_records_pending_entry(self, tmp_path):
        response = FakeResponse(
            '<div id="messagetext" class="alert_info">'
            "<p>新主题需要审核，您的帖子通过审核后才能显示</p></div>"
        )
        session = ScriptedSession([
            ("GET", "action=newthread", FakeResponse(NEWTHREAD_PAGE)),
            ("POST", "topicsubmit=yes", response),
        ])
        _post_news(_make_poster(session), tmp_path, "news_x", "Minecraft 26.4 快照 9")

        records = json.loads((tmp_path / ".mcbbs_threads.json").read_text(encoding="utf-8"))
        record = records["news_x"]
        assert record["state"] == "pending"
        assert record["tid"] is None
        assert record["news_type"] == "java_snapshot"
        assert record["color"] == "#3C9D40"
        # 快照属于「版本资讯」，按版规最新版本加粗
        assert record["bold"] is True
        assert record["fid"] == 45
        assert record["title"] == "Minecraft 26.4 快照 9 [AI翻译]"
        assert record["posted_at"]

    def test_release_post_marks_bold(self, tmp_path):
        response = FakeResponse(
            "<script>window.location.href ='thread-7600-1-1.html';</script>"
            "<p>新主题需要审核，您的帖子通过审核后才能显示</p>"
        )
        session = ScriptedSession([
            ("GET", "action=newthread", FakeResponse(NEWTHREAD_PAGE)),
            ("POST", "topicsubmit=yes", response),
        ])
        _post_news(_make_poster(session), tmp_path, "news_r", "Minecraft Java Edition 26.4 is out")
        record = json.loads((tmp_path / ".mcbbs_threads.json").read_text(encoding="utf-8"))["news_r"]
        assert record["news_type"] == "java_release"
        assert record["color"] == "#EE1B2E"
        assert record["bold"] is True
        assert record["tid"] == 7600

    def test_type_without_color_is_not_registered(self, tmp_path):
        response = FakeResponse("<p>新主题需要审核，您的帖子通过审核后才能显示</p>")
        session = ScriptedSession([
            ("GET", "action=newthread", FakeResponse(NEWTHREAD_PAGE)),
            ("POST", "topicsubmit=yes", response),
        ])
        _post_news(_make_poster(session), tmp_path, "news_c", "某条时评",
                   message="[module_commentary_header]\n正文")
        assert not (tmp_path / ".mcbbs_threads.json").exists()

    def test_disabled_highlight_writes_nothing(self, tmp_path):
        response = FakeResponse("<p>新主题需要审核，您的帖子通过审核后才能显示</p>")
        session = ScriptedSession([
            ("GET", "action=newthread", FakeResponse(NEWTHREAD_PAGE)),
            ("POST", "topicsubmit=yes", response),
        ])
        _post_news(_make_poster(session, highlight={"enabled": False}), tmp_path,
                   "news_x", "Minecraft 26.4 快照 9")
        assert not (tmp_path / ".mcbbs_threads.json").exists()


class TestCredentials:
    """账号密码可以从 .env 走环境变量覆盖；缺失时提示要说清缺哪个。"""

    def test_env_overrides_empty_password(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"mcbbs": {"username": "from_file", "password": ""}}),
            encoding="utf-8",
        )
        fake_value = "test-value"
        monkeypatch.setenv("MCBBS_USERNAME", "from_env")
        monkeypatch.setenv("MCBBS_PASSWORD", fake_value)

        cfg = poster.load_poster_config(str(config_path))

        assert cfg["username"] == "from_env"
        assert cfg["password"] == fake_value

    def test_hint_names_the_missing_variable(self, monkeypatch):
        monkeypatch.delenv("MCBBS_USERNAME", raising=False)
        monkeypatch.delenv("MCBBS_PASSWORD", raising=False)

        hint = poster._login_config_hint({"username": "newspaper", "password": ""})

        assert "缺 MCBBS_PASSWORD" in hint
        assert "MCBBS_USERNAME" not in hint.split("；")[0]
        assert ".env" in hint

    def test_hint_reports_loaded_env(self, monkeypatch):
        monkeypatch.setenv("MCBBS_PASSWORD", "test-value")
        hint = poster._login_config_hint({"username": "", "password": ""})
        assert "未读到" not in hint


class TestHighlightConfig:
    def test_partial_config_keeps_defaults(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"mcbbs": {"highlight": {"colors": {"normal": "#EE5023"}}}}),
            encoding="utf-8",
        )
        cfg = poster.load_poster_config(str(config_path))
        assert cfg["highlight"]["colors"]["normal"] == "#EE5023"
        assert cfg["highlight"]["colors"]["java_release"] == "#EE1B2E"
        assert cfg["highlight"]["max_attempts"] == poster.DEFAULT_HIGHLIGHT["max_attempts"]

    def test_config_with_bom_keeps_mcbbs_settings(self, tmp_path):
        """带 BOM 的 config.json 不能让 mcbbs 段整段回落默认值（账号、版块、色板全丢）。"""
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"mcbbs": {"forum_fid": 45, "highlight": {"enabled": False}}}),
            encoding="utf-8-sig",
        )

        cfg = poster.load_poster_config(str(config_path))

        assert cfg["forum_fid"] == 45
        assert cfg["highlight"]["enabled"] is False


    def test_bad_types_fall_back_to_defaults(self):
        merged = poster._merge_highlight({
            "enabled": "yes",
            "max_attempts": "3",
            "max_age_hours": True,
            "colors": {"normal": 8},
            "bold_types": "java_release",
        })
        assert merged["enabled"] is True
        assert merged["max_attempts"] == poster.DEFAULT_HIGHLIGHT["max_attempts"]
        assert merged["max_age_hours"] == poster.DEFAULT_HIGHLIGHT["max_age_hours"]
        assert merged["colors"]["normal"] == "#EC1282"
        assert merged["bold_types"] == poster.DEFAULT_HIGHLIGHT["bold_types"]

    def test_non_dict_section_ignored(self):
        assert poster._merge_highlight(["nope"])["colors"] == poster.DEFAULT_HIGHLIGHT["colors"]
