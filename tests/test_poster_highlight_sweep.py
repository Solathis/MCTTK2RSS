"""test_poster_highlight_sweep.py — 补高亮：列表补 tid、游客探测、放弃条件与取消加粗"""
import json
import time

import pytest

import main
import poster
from poster import MCBBSPoster

BASE = "https://www.mcbbs.co"
FORMHASH = "abc12345"
OK_RESPONSE = "<script>succeedhandle_mods('u',{},{});</script>"
FAIL_RESPONSE = "<script>errorhandle_mods('抱歉，您没有权限执行此操作', {});</script>"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """翻页/重试的 sleep 在测试里没有意义，直接跳过。"""
    monkeypatch.setattr(poster.time, "sleep", lambda *_: None)


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code
        self.headers = {}
        self.content = b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise poster.requests.HTTPError(f"HTTP {self.status_code}")


class SweepSession:
    """列表页 + 高亮提交的假会话：按方法区分，高亮响应按顺序取。"""

    def __init__(self, list_html="", highlight_responses=None):
        self.list_html = list_html
        self.highlight_responses = list(highlight_responses or [])
        self.headers = {}
        self.gets = []
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse(self.list_html)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.highlight_responses:
            return self.highlight_responses.pop(0)
        return FakeResponse(OK_RESPONSE)


def _record(**overrides):
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    record = {
        "tid": 6808,
        "fid": 45,
        "url": f"{BASE}/thread-6808-1-1.html",
        "title": "Minecraft 26.4 快照 9 [AI翻译]",
        "news_type": "java_snapshot",
        "color": "#3C9D40",
        "bold": False,
        "state": "pending",
        "posted_at": now,
        "updated_at": now,
        "attempts": 0,
        "last_error": "",
    }
    record.update(overrides)
    return record


def _write_records(tmp_path, records) -> str:
    path = tmp_path / ".mcbbs_threads.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _make_poster(session=None) -> MCBBSPoster:
    poster_obj = MCBBSPoster({
        "base_url": BASE, "forum_fid": 45, "username": "newspaper", "password": "secret",
    })
    poster_obj.session = session
    poster_obj.formhash = FORMHASH
    return poster_obj


def _read_records(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _list_html(*rows) -> str:
    """拼一段「我的帖子」列表：rows 是 (tid, 标题) 或 (tid, 标题, 链接样式)。"""
    parts = []
    for row in rows:
        tid, title = row[0], row[1]
        style = row[2] if len(row) > 2 else "query"
        href = (f"thread-{tid}-1-1.html" if style == "static"
                else f"forum.php?mod=viewthread&amp;tid={tid}")
        parts.append(f'<tbody><tr><th><a href="{href}" class="s xst">{title}</a></th>'
                     f'<td class="num"><a href="{href}">3</a></td></tr></tbody>')
    return "".join(parts)


class TestMyThreadsParser:
    def test_parses_both_link_styles_and_dedupes(self):
        html = _list_html((6816, "TwitchCon 揭晓 2027 年首个游戏更新"), (6815, "The Humblers 觉醒"))
        threads = poster._parse_my_threads(html)
        assert threads == [
            (6816, "TwitchCon 揭晓 2027 年首个游戏更新"),
            (6815, "The Humblers 觉醒"),
        ]

    def test_static_links_and_entities(self):
        html = ('<a href="thread-6808-1-1.html" class="s xst">'
                "Minecraft 26.4 &amp; 快照 9 [AI翻译]</a>")
        assert poster._parse_my_threads(html) == [(6808, "Minecraft 26.4 & 快照 9 [AI翻译]")]

    def test_ignores_links_without_title_text(self):
        html = ('<a href="forum.php?mod=viewthread&tid=6816&highlight=x"></a>'
                '<a href="forum.php?mod=redirect&tid=6816&goto=lastpost">12</a>')
        assert poster._parse_my_threads(html) == []

    def test_unknown_markup_returns_empty(self):
        assert poster._parse_my_threads("<html><body>登录后查看</body></html>") == []


class TestThreadVisibility:
    def test_visible_page(self, monkeypatch):
        monkeypatch.setattr(poster.requests, "Session", lambda: SweepSession(
            '<span id="thread_subject">标题</span>'))
        assert poster.thread_is_public(BASE, 6808) is True

    def test_404_means_not_visible(self, monkeypatch):
        class NotFoundSession(SweepSession):
            def get(self, url, **kwargs):
                return FakeResponse("<title>提示信息</title>", status_code=404)

        monkeypatch.setattr(poster.requests, "Session", lambda: NotFoundSession())
        assert poster.thread_is_public(BASE, 999999) is False

    def test_page_without_thread_subject_means_not_visible(self, monkeypatch):
        monkeypatch.setattr(poster.requests, "Session", lambda: SweepSession(
            "<h1>提示信息</h1><p>请先登录</p>"))
        assert poster.thread_is_public(BASE, 6808) is False

    def test_network_error_is_unknown(self, monkeypatch):
        class BoomSession(SweepSession):
            def get(self, url, **kwargs):
                raise poster.requests.ConnectionError("boom")

        monkeypatch.setattr(poster.requests, "Session", lambda: BoomSession())
        assert poster.thread_is_public(BASE, 6808) is None


class TestSweep:
    def test_still_in_audit_does_not_login(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: False)
        monkeypatch.setattr(MCBBSPoster, "login", lambda self: (_ for _ in ()).throw(
            AssertionError("还没过审就登录了")))
        path = _write_records(tmp_path, {"news_x": _record()})

        stats = _make_poster().process_pending_highlights(path)

        assert stats["pending"] == 1
        assert stats["highlighted"] == 0
        record = _read_records(path)["news_x"]
        assert record["state"] == "pending"
        assert record["attempts"] == 0
        assert "审核" in record["last_error"]

    def test_highlights_once_approved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        session = SweepSession()
        path = _write_records(tmp_path, {"news_x": _record()})

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["highlighted"] == 1
        assert len(session.posts) == 1
        assert session.posts[0][1]["data"]["moderate[]"] == "6808"
        record = _read_records(path)["news_x"]
        assert record["state"] == "done"
        assert record["highlighted_at"]
        assert record["last_error"] == ""

    def test_probe_error_keeps_attempt_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: None)
        path = _write_records(tmp_path, {"news_x": _record()})

        _make_poster(SweepSession()).process_pending_highlights(path)

        record = _read_records(path)["news_x"]
        assert record["attempts"] == 0
        assert record["state"] == "pending"
        assert "网络" in record["last_error"]

    def test_failure_counts_attempt_and_keeps_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        session = SweepSession(highlight_responses=[FakeResponse(FAIL_RESPONSE)])
        path = _write_records(tmp_path, {"news_x": _record()})

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["failed"] == 1
        record = _read_records(path)["news_x"]
        assert record["state"] == "pending"
        assert record["attempts"] == 1
        assert "没有权限" in record["last_error"]

    def test_gives_up_after_max_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        limit = poster.DEFAULT_HIGHLIGHT["max_attempts"]
        session = SweepSession(highlight_responses=[FakeResponse(FAIL_RESPONSE)])
        path = _write_records(tmp_path, {"news_x": _record(attempts=limit - 1)})

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["gave_up"] == 1
        record = _read_records(path)["news_x"]
        assert record["state"] == "gave_up"

    def test_gives_up_when_waiting_too_long(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: False)
        old = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - (poster.DEFAULT_HIGHLIGHT["max_age_hours"] + 1) * 3600),
        )
        path = _write_records(tmp_path, {"news_x": _record(posted_at=old)})

        stats = _make_poster(SweepSession()).process_pending_highlights(path)

        assert stats["gave_up"] == 1
        assert _read_records(path)["news_x"]["state"] == "gave_up"

    def test_logs_in_only_when_something_is_ready(self, tmp_path, monkeypatch):
        logged_in = []

        def fake_login(self):
            logged_in.append(1)
            self.session = SweepSession()
            return True

        monkeypatch.setattr(MCBBSPoster, "login", fake_login)
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: False)
        waiting = _write_records(tmp_path, {"news_x": _record()})
        _make_poster(None).process_pending_highlights(waiting)
        assert logged_in == []

        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        ready = _write_records(tmp_path, {"news_x": _record()})
        _make_poster(None).process_pending_highlights(ready)
        assert logged_in == [1]

    def test_empty_records_touches_no_network(self, tmp_path):
        poster_obj = _make_poster(None)
        stats = poster_obj.process_pending_highlights(str(tmp_path / ".mcbbs_threads.json"))
        assert stats == {"pending": 0, "highlighted": 0, "unbolded": 0,
                         "gave_up": 0, "failed": 0}
        assert poster_obj.session is None

    def test_network_error_while_highlighting_counts_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)

        def boom(self, tid, color, bold=False, fid=None):
            raise poster.requests.ConnectionError("boom")

        monkeypatch.setattr(MCBBSPoster, "highlight_thread", boom)
        path = _write_records(tmp_path, {"news_x": _record()})

        stats = _make_poster(SweepSession()).process_pending_highlights(path)

        assert stats["failed"] == 1
        record = _read_records(path)["news_x"]
        assert record["state"] == "pending"
        assert record["attempts"] == 1
        assert "高亮请求失败" in record["last_error"]

    def test_probe_results_saved_when_login_fails(self, tmp_path, monkeypatch):
        def deny_login(self):
            raise RuntimeError("验证码识别率过低")

        monkeypatch.setattr(MCBBSPoster, "login", deny_login)
        monkeypatch.setattr(poster, "thread_is_public",
                            lambda base, tid, **kwargs: tid == 7001)
        path = _write_records(tmp_path, {
            "waiting": _record(tid=7000),
            "ready": _record(tid=7001),
        })

        with pytest.raises(RuntimeError, match="验证码"):
            _make_poster(None).process_pending_highlights(path)

        records = _read_records(path)
        assert "仍在审核中" in records["waiting"]["last_error"]
        assert records["ready"]["state"] == "pending"


class TestTidResolution:
    def test_matches_title_from_my_threads(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        html = _list_html((6900, "Minecraft 26.4 快照 9 [AI翻译]"), (6899, "别的帖子"))
        session = SweepSession(list_html=html)
        path = _write_records(tmp_path, {
            "news_x": _record(tid=None, url="", title="Minecraft 26.4 快照 9 [AI翻译]"),
        })

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["highlighted"] == 1
        record = _read_records(path)["news_x"]
        assert record["tid"] == 6900
        assert record["tid_source"] == "标题匹配"
        assert session.posts[0][1]["data"]["moderate[]"] == "6900"

    def test_falls_back_to_newest_tid_when_title_changed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        html = _list_html((6901, "标题被版主改过"), (6700, "旧帖子"))
        session = SweepSession(list_html=html)
        path = _write_records(tmp_path, {
            "news_x": _record(tid=None, url="", title="原始标题 [AI翻译]"),
            "news_old": _record(tid=6700, state="done"),
        })

        _make_poster(session).process_pending_highlights(path)

        record = _read_records(path)["news_x"]
        assert record["tid"] == 6901
        assert record["tid_source"] == "列表新增兜底"

    def test_missing_tid_counts_attempt_and_waits(self, tmp_path, monkeypatch):
        html = _list_html((6700, "旧帖子"))
        session = SweepSession(list_html=html)
        path = _write_records(tmp_path, {
            "news_x": _record(tid=None, url="", title="原始标题 [AI翻译]"),
            "news_old": _record(tid=6700, state="done"),
        })

        _make_poster(session).process_pending_highlights(path)

        record = _read_records(path)["news_x"]
        assert record["tid"] is None
        assert record["attempts"] == 1
        assert record["state"] == "pending"
        assert session.posts == []

    def test_unparseable_list_dumps_page_and_keeps_pending(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        monkeypatch.setattr(poster, "_login_debug_dir", lambda: str(log_dir))
        session = SweepSession(list_html="<html>登录后查看</html>")
        path = _write_records(tmp_path, {"news_x": _record(tid=None, title="标题")})

        _make_poster(session).process_pending_highlights(path)

        assert _read_records(path)["news_x"]["state"] == "pending"
        assert list(log_dir.glob("mcbbs_my-threads_*.html")), "解析失败时应把列表页落盘"


class TestUnboldPredecessors:
    def test_new_release_unbolds_previous_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        session = SweepSession()
        path = _write_records(tmp_path, {
            "news_new": _record(tid=7000, news_type="java_release", color="#EE1B2E", bold=True),
            "news_old": _record(tid=6900, news_type="java_release", color="#EE1B2E",
                                bold=True, state="done"),
        })

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["highlighted"] == 1
        assert stats["unbolded"] == 1
        assert _read_records(path)["news_old"]["bold"] is False
        unbold_data = session.posts[1][1]["data"]
        assert unbold_data["moderate[]"] == "6900"
        assert unbold_data["highlight_color"] == "1"
        assert unbold_data["highlight_style[1]"] == "0"

    def test_other_types_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        session = SweepSession()
        path = _write_records(tmp_path, {
            "news_new": _record(tid=7000, news_type="java_release", color="#EE1B2E", bold=True),
            "news_old": _record(tid=6900, news_type="java_snapshot", color="#3C9D40",
                                bold=True, state="done"),
        })

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["unbolded"] == 0
        assert len(session.posts) == 1
        assert _read_records(path)["news_old"]["bold"] is True

    def test_non_bold_new_post_unbolds_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "thread_is_public", lambda base, tid, **kwargs: True)
        session = SweepSession()
        path = _write_records(tmp_path, {
            "news_new": _record(tid=7000),
            "news_old": _record(tid=6900, news_type="java_snapshot", color="#3C9D40",
                                bold=True, state="done"),
        })

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["unbolded"] == 0
        assert _read_records(path)["news_old"]["bold"] is True


class TestMainWiring:
    def _config(self, tmp_path, enabled=True):
        return {
            "output": {"save_dir": str(tmp_path)},
            "mcbbs": {"enabled": enabled},
            "retry": {"backfill_unposted": True},
        }

    def test_pending_highlights_detected(self, tmp_path):
        config = self._config(tmp_path)
        assert main._has_pending_mcbbs_highlights(config) is False

        _write_records(tmp_path, {"news_x": _record()})
        assert main._has_pending_mcbbs_highlights(config) is True

        _write_records(tmp_path, {"news_x": _record(state="done")})
        assert main._has_pending_mcbbs_highlights(config) is False

    def test_disabled_mcbbs_skips_highlights(self, tmp_path):
        _write_records(tmp_path, {"news_x": _record()})
        assert main._has_pending_mcbbs_highlights(self._config(tmp_path, enabled=False)) is False

    def test_post_only_sweeps_without_posting(self, tmp_path, monkeypatch):
        class RecordingPoster:
            instances = []

            def __init__(self, config):
                self.swept = []
                self.logged_in = 0
                self.posted = []
                RecordingPoster.instances.append(self)

            def process_pending_highlights(self, records_path):
                self.swept.append(records_path)
                return {"pending": 1, "highlighted": 1}

            def login(self):
                self.logged_in += 1
                return True

            def post_news_file(self, stem, *args, **kwargs):
                self.posted.append(stem)
                return "https://example.invalid/x"

        monkeypatch.setattr(poster, "MCBBSPoster", RecordingPoster)
        monkeypatch.setattr(poster, "load_poster_config",
                            lambda path=None: {"base_url": BASE, "forum_fid": 45})
        _write_records(tmp_path, {"news_x": _record()})

        result = main.run_post_only(self._config(tmp_path))

        assert result == {"success": 0, "failed": 0, "urls": []}
        assert RecordingPoster.instances[-1].swept == [
            main.thread_records_path(str(tmp_path))
        ]
        assert RecordingPoster.instances[-1].posted == []
        assert RecordingPoster.instances[-1].logged_in == 0

    def test_run_post_sweeps_even_without_new_articles(self, tmp_path, monkeypatch):
        class RecordingPoster:
            instances = []

            def __init__(self, config):
                self.swept = []
                self.posted = []
                RecordingPoster.instances.append(self)

            def process_pending_highlights(self, records_path):
                self.swept.append(records_path)
                return {"pending": 1, "highlighted": 1}

            def login(self):
                return True

            def post_news_file(self, stem, *args, **kwargs):
                self.posted.append(stem)
                return "https://example.invalid/x"

        monkeypatch.setattr(poster, "MCBBSPoster", RecordingPoster)
        monkeypatch.setattr(poster, "load_poster_config",
                            lambda path=None: {"base_url": BASE, "forum_fid": 45})
        _write_records(tmp_path, {"news_x": _record()})

        result = main.run_post([], self._config(tmp_path))

        assert result == {"success": 0, "failed": 0, "urls": []}
        assert RecordingPoster.instances[-1].swept
        assert RecordingPoster.instances[-1].posted == []
