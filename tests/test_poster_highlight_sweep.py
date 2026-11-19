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


@pytest.fixture(autouse=True)
def _no_forum_scan(monkeypatch):
    """取消旧版加粗要读版块列表；测试里默认给空列表，避免真联网。"""
    monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads", lambda self, pages=3: [])


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
        "sortid_map": {"java_snapshot": 4, "java_release": 4, "bedrock_beta": 20},
    })
    poster_obj.session = session
    poster_obj.formhash = FORMHASH
    return poster_obj


def _probe(visible, forum=None, fid=None):
    """构造探测结果；forum 是面包屑文本，用来验证版块排除。"""
    return poster.ThreadProbe(visible, forum_fid=fid, crumb=forum or "")


def _visible(*_args, **_kwargs):
    return _probe(True)


def _invisible(*_args, **_kwargs):
    return _probe(False)


def _unknown(*_args, **_kwargs):
    return _probe(None)


def _read_records(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _list_html(*rows) -> str:
    """拼一段「我的帖子」列表：rows 是 (tid, 标题[, 样式[, 版块名[, 删除线]]])。"""
    parts = []
    for row in rows:
        tid, title = row[0], row[1]
        style = row[2] if len(row) > 2 else "query"
        forum = row[3] if len(row) > 3 else ""
        struck = row[4] if len(row) > 4 else False
        href = (f"thread-{tid}-1-1.html" if style == "static"
                else f"forum.php?mod=viewthread&amp;tid={tid}")
        title_class = ' class="s xst recy"' if struck else ' class="s xst"'
        forum_cell = (f'<td><a href="forum.php?mod=forumdisplay&amp;fid=45" class="xg1">'
                      f"{forum}</a></td>" if forum else "")
        parts.append(f"<tbody><tr><th><a href=\"{href}\"{title_class}>{title}</a></th>"
                     f'<td class="num"><a href="{href}">3</a></td>{forum_cell}</tr></tbody>')
    return "".join(parts)


class TestMyThreadsParser:
    def test_parses_both_link_styles_and_dedupes(self):
        html = _list_html((6816, "TwitchCon 揭晓 2027 年首个游戏更新"), (6815, "The Humblers 觉醒"))
        threads = poster._parse_my_threads(html)
        assert [(item.tid, item.title) for item in threads] == [
            (6816, "TwitchCon 揭晓 2027 年首个游戏更新"),
            (6815, "The Humblers 觉醒"),
        ]
        assert all(not item.recy for item in threads)

    def test_static_links_and_entities(self):
        html = ('<a href="thread-6808-1-1.html" class="s xst">'
                "Minecraft 26.4 &amp; 快照 9 [AI翻译]</a>")
        threads = poster._parse_my_threads(html)
        assert [(item.tid, item.title) for item in threads] == [
            (6808, "Minecraft 26.4 & 快照 9 [AI翻译]")
        ]

    def test_recy_class_marks_deleted(self):
        """站点的删除线就是 class="recy"（style_3_home_space.css 里定义了 line-through）。"""
        html = _list_html((6710, "Minecraft 电影建造挑战投票活动 [AI翻译]", "query", "", True))
        assert poster._parse_my_threads(html)[0].recy is True

    def test_inline_line_through_also_marks_deleted(self):
        html = ('<a href="thread-6711-1-1.html" class="s xst" '
                'style="text-decoration: line-through">旧帖</a>')
        assert poster._parse_my_threads(html)[0].recy is True

    def test_del_wrapper_marks_deleted(self):
        html = '<a href="thread-6712-1-1.html" class="s xst"><del>旧帖</del></a>'
        assert poster._parse_my_threads(html)[0].recy is True

    def test_forum_name_and_fid_taken_from_row(self):
        html = _list_html((6713, "帖子标题", "query", "块讯回收区"))
        item = poster._parse_my_threads(html)[0]
        assert item.forum_name == "块讯回收区"
        assert item.forum_fid == 45
        assert item.recy is False

    def test_ignores_links_without_title_text(self):
        html = ('<a href="forum.php?mod=viewthread&tid=6816&highlight=x"></a>'
                '<a href="forum.php?mod=redirect&tid=6816&goto=lastpost">12</a>')
        assert poster._parse_my_threads(html) == []

    def test_unknown_markup_returns_empty(self):
        assert poster._parse_my_threads("<html><body>登录后查看</body></html>") == []


class TestThreadProbe:
    def test_visible_page(self, monkeypatch):
        monkeypatch.setattr(poster.requests, "Session", lambda: SweepSession(
            '<div id="pt"><a href="forum-news-1.html">幻翼块讯</a> '
            '<a href="forum.php?mod=forumdisplay&amp;fid=45&amp;filter=typeid">[块讯]</a></div>'
            '<span id="thread_subject">标题</span>'))
        probe = poster.probe_thread(BASE, 6808)
        assert probe.visible is True
        assert probe.forum_fid == 45
        assert "幻翼块讯" in probe.crumb

    def test_404_means_not_visible(self, monkeypatch):
        class NotFoundSession(SweepSession):
            def get(self, url, **kwargs):
                return FakeResponse("<title>提示信息</title>", status_code=404)

        monkeypatch.setattr(poster.requests, "Session", lambda: NotFoundSession())
        assert poster.probe_thread(BASE, 999999).visible is False

    def test_page_without_thread_subject_means_not_visible(self, monkeypatch):
        monkeypatch.setattr(poster.requests, "Session", lambda: SweepSession(
            "<h1>提示信息</h1><p>请先登录</p>"))
        assert poster.probe_thread(BASE, 6808).visible is False

    def test_network_error_is_unknown(self, monkeypatch):
        class BoomSession(SweepSession):
            def get(self, url, **kwargs):
                raise poster.requests.ConnectionError("boom")

        monkeypatch.setattr(poster.requests, "Session", lambda: BoomSession())
        assert poster.probe_thread(BASE, 6808).visible is None


class TestSweep:
    def test_still_in_audit_does_not_login(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _invisible)
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
        monkeypatch.setattr(poster, "probe_thread", _visible)
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

    def test_drops_threads_moved_to_excluded_forum(self, tmp_path, monkeypatch):
        """帖子被移到「块讯回收区」后仍然对外可见，但绝不该上高亮。"""
        monkeypatch.setattr(poster, "probe_thread",
                            lambda base, tid, **kwargs: _probe(True, forum="块讯回收区"))
        session = SweepSession()
        path = _write_records(tmp_path, {"news_x": _record()})

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["dropped"] == 1
        assert stats["highlighted"] == 0
        assert session.posts == []
        record = _read_records(path)["news_x"]
        assert record["state"] == "dropped"
        assert "块讯回收区" in record["last_error"]

    def test_threads_in_normal_forum_are_highlighted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread",
                            lambda base, tid, **kwargs: _probe(True, forum="幻翼块讯"))
        session = SweepSession()
        path = _write_records(tmp_path, {"news_x": _record()})

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["highlighted"] == 1
        assert stats["dropped"] == 0

    def test_probe_error_keeps_attempt_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _unknown)
        path = _write_records(tmp_path, {"news_x": _record()})

        _make_poster(SweepSession()).process_pending_highlights(path)

        record = _read_records(path)["news_x"]
        assert record["attempts"] == 0
        assert record["state"] == "pending"
        assert "网络" in record["last_error"]

    def test_failure_counts_attempt_and_keeps_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _visible)
        session = SweepSession(highlight_responses=[FakeResponse(FAIL_RESPONSE)])
        path = _write_records(tmp_path, {"news_x": _record()})

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["failed"] == 1
        record = _read_records(path)["news_x"]
        assert record["state"] == "pending"
        assert record["attempts"] == 1
        assert "没有权限" in record["last_error"]

    def test_gives_up_after_max_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _visible)
        limit = poster.DEFAULT_HIGHLIGHT["max_attempts"]
        session = SweepSession(highlight_responses=[FakeResponse(FAIL_RESPONSE)])
        path = _write_records(tmp_path, {"news_x": _record(attempts=limit - 1)})

        stats = _make_poster(session).process_pending_highlights(path)

        assert stats["gave_up"] == 1
        record = _read_records(path)["news_x"]
        assert record["state"] == "gave_up"

    def test_gives_up_when_waiting_too_long(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _invisible)
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
        monkeypatch.setattr(poster, "probe_thread", _invisible)
        waiting = _write_records(tmp_path, {"news_x": _record()})
        _make_poster(None).process_pending_highlights(waiting)
        assert logged_in == []

        monkeypatch.setattr(poster, "probe_thread", _visible)
        ready = _write_records(tmp_path, {"news_x": _record()})
        _make_poster(None).process_pending_highlights(ready)
        assert logged_in == [1]

    def test_empty_records_touches_no_network(self, tmp_path):
        poster_obj = _make_poster(None)
        stats = poster_obj.process_pending_highlights(str(tmp_path / ".mcbbs_threads.json"))
        assert stats == {"pending": 0, "highlighted": 0, "unbolded": 0,
                         "gave_up": 0, "failed": 0, "dropped": 0}
        assert poster_obj.session is None

    def test_network_error_while_highlighting_counts_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _visible)

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
        monkeypatch.setattr(poster, "probe_thread",
                            lambda base, tid, **kwargs: _probe(tid == 7001))
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
        monkeypatch.setattr(poster, "probe_thread", _visible)
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
        monkeypatch.setattr(poster, "probe_thread", _visible)
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


class TestUnboldOlderVersions:
    """同一分类里只保留最新一篇加粗，判断依据是版块列表里的真实状态。"""

    def _rows(self, *rows):
        return [poster.ForumThread(tid=tid, title=f"t{tid}", typeid=typeid,
                                   color=color, bold=bold)
                for tid, typeid, color, bold in rows]

    def _poster_with_rows(self, rows):
        poster_obj = _make_poster(SweepSession())
        poster_obj.fetch_forum_threads = lambda pages=3: rows
        return poster_obj

    def test_unbolds_other_bold_threads_in_same_category(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _visible)
        path = _write_records(tmp_path, {
            "news_new": _record(tid=7000, news_type="java_release",
                                color="#EE1B2E", bold=True, typeid=4),
            "news_old": _record(tid=6900, news_type="java_snapshot",
                                color="#3C9D40", bold=True, state="done", typeid=4),
        })
        poster_obj = self._poster_with_rows(self._rows(
            (7000, 4, "#EE1B2E", True),
            (6900, 4, "#3C9D40", True),
            (6700, 20, "#EE1B2E", True),   # 另一个分类，不动
            (6600, 4, "", False),          # 本来就不加粗
        ))

        stats = poster_obj.process_pending_highlights(path)

        assert stats["highlighted"] == 1
        assert stats["unbolded"] == 1
        session = poster_obj.session
        assert len(session.posts) == 2
        unbold_data = session.posts[1][1]["data"]
        assert unbold_data["moderate[]"] == "6900"
        assert unbold_data["highlight_color"] == "4"
        assert unbold_data["highlight_style[1]"] == "0"
        assert _read_records(path)["news_old"]["bold"] is False

    def test_recordless_bold_thread_still_unbolded(self, tmp_path, monkeypatch):
        """站点上手工加粗、本地没有记录的历史帖同样要取消加粗。"""
        monkeypatch.setattr(poster, "probe_thread", _visible)
        path = _write_records(tmp_path, {
            "news_new": _record(tid=7000, news_type="java_release",
                                color="#EE1B2E", bold=True, typeid=4),
        })
        poster_obj = self._poster_with_rows(self._rows(
            (7000, 4, "#EE1B2E", True),
            (6500, 4, "#3C9D40", True),
        ))

        stats = poster_obj.process_pending_highlights(path)

        assert stats["unbolded"] == 1
        assert poster_obj.session.posts[1][1]["data"]["moderate[]"] == "6500"

    def test_no_typeid_skips_scan(self, tmp_path, monkeypatch):
        """没有分类信息时没法判断「同一分类」，宁可不取消。"""
        monkeypatch.setattr(poster, "probe_thread", _visible)
        path = _write_records(tmp_path, {"news_new": _record(tid=7000, bold=True)})
        poster_obj = self._poster_with_rows(self._rows((6900, 4, "#3C9D40", True)))

        stats = poster_obj.process_pending_highlights(path)

        assert stats["unbolded"] == 0
        assert len(poster_obj.session.posts) == 1

    def test_non_bold_category_skips_scan(self, tmp_path, monkeypatch):
        """块讯（typeid 16）不在 bold_types 覆盖的分类里，别去动别人的加粗。"""
        monkeypatch.setattr(poster, "probe_thread", _visible)
        path = _write_records(tmp_path, {
            "news_new": _record(tid=7000, news_type="normal", color="#EC1282",
                                bold=True, typeid=16),
        })
        poster_obj = self._poster_with_rows(self._rows((6900, 16, "#2B65B7", True)))

        stats = poster_obj.process_pending_highlights(path)

        assert stats["unbolded"] == 0
        assert len(poster_obj.session.posts) == 1

    def test_current_not_bold_skips_scan(self, tmp_path, monkeypatch):
        """自己都没加粗，就没必要去收敛同分类的加粗。"""
        monkeypatch.setattr(poster, "probe_thread", _visible)
        path = _write_records(tmp_path, {"news_new": _record(tid=7000, bold=False, typeid=4)})
        poster_obj = self._poster_with_rows(self._rows((6900, 4, "#3C9D40", True)))

        stats = poster_obj.process_pending_highlights(path)

        assert stats["unbolded"] == 0
        assert len(poster_obj.session.posts) == 1

    def test_forum_list_failure_is_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "probe_thread", _visible)
        path = _write_records(tmp_path, {
            "news_new": _record(tid=7000, news_type="java_release",
                                color="#EE1B2E", bold=True, typeid=4),
        })
        poster_obj = _make_poster(SweepSession())

        def boom(pages=3):
            raise poster.requests.ConnectionError("boom")

        poster_obj.fetch_forum_threads = boom

        stats = poster_obj.process_pending_highlights(path)

        assert stats["highlighted"] == 1
        assert stats["unbolded"] == 0


class TestForumThreadsParser:
    """版块列表是唯一能看到高亮样式的地方：标题链接上的内联 style。"""

    def _row(self, tid, title, typeid=4, type_name="Java版资讯", style=""):
        style_attr = f' style="{style}"' if style else ""
        return (
            f'<tbody id="normalthread_{tid}"><tr><th class="new">'
            f'<em>[<a href="forum.php?mod=forumdisplay&amp;fid=45&amp;filter=typeid'
            f'&amp;typeid={typeid}">{type_name}</a>]</em> '
            f'<a href="thread-{tid}-1-1.html"{style_attr} class="s xst">{title}</a>'
            f"</th><td class=\"num\"><a href=\"thread-{tid}-1-1.html\">3</a></td></tr></tbody>"
        )

    def test_reads_colour_and_bold_from_inline_style(self):
        html = self._row(6773, "Minecraft 26.4 快照 8 [AI翻译]",
                         style="font-weight: bold;color: #3C9D40;")
        row = poster._parse_forum_threads(html)[0]
        assert (row.tid, row.typeid, row.color, row.bold) == (6773, 4, "#3C9D40", True)
        assert row.type_name == "Java版资讯"
        assert row.title == "Minecraft 26.4 快照 8 [AI翻译]"

    def test_plain_thread_has_no_highlight(self):
        row = poster._parse_forum_threads(self._row(6800, "普通帖子"))[0]
        assert (row.color, row.bold) == ("", False)

    def test_coloured_but_not_bold(self):
        html = self._row(6801, "只有颜色", style="color: #EC1282;")
        row = poster._parse_forum_threads(html)[0]
        assert row.color == "#EC1282"
        assert row.bold is False

    def test_other_category_typeid(self):
        html = self._row(6737, "Minecraft: 基岩版 26.62 热修复更新日志 [AI翻译]",
                         typeid=20, type_name="基岩版资讯",
                         style="font-weight: bold;color: #EE1B2E;")
        row = poster._parse_forum_threads(html)[0]
        assert (row.typeid, row.color, row.bold) == (20, "#EE1B2E", True)

    def test_reads_rows_from_later_pages(self):
        """翻到第 2 页时标题链接变成 thread-6720-1-2.html，只认 -1-1 会把整页读空。"""
        html = (
            '<tbody id="normalthread_6720"><tr><th class="new">'
            '<em>[<a href="forum.php?mod=forumdisplay&amp;fid=45&amp;filter=typeid'
            '&amp;typeid=16">块讯</a>]</em> '
            '<a href="thread-6720-1-2.html" style="color: #EC1282;" class="s xst">旧帖</a>'
            "</th></tr></tbody>"
        )
        row = poster._parse_forum_threads(html)[0]
        assert (row.tid, row.typeid, row.color, row.bold) == (6720, 16, "#EC1282", False)

    def test_unknown_markup_returns_empty(self):
        assert poster._parse_forum_threads("<html>no rows</html>") == []


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
