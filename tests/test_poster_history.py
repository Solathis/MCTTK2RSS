"""test_poster_history.py — 历史帖登记（legacy）、限量提升与 CLI 补高亮入口"""
import argparse
import json
import sys

import pytest

import poster
from poster import MCBBSPoster

BASE = "https://www.mcbbs.co"
FORMHASH = "abc12345"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(poster.time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _no_forum_scan(monkeypatch):
    """历史登记会读版块列表拿真实颜色/加粗；测试里默认给空列表，避免真联网。"""
    monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads", lambda self, pages=15: [])


def _make_poster(session=None) -> MCBBSPoster:
    poster_obj = MCBBSPoster({
        "base_url": BASE, "forum_fid": 45, "username": "newspaper", "password": "secret",
    })
    poster_obj.session = session
    poster_obj.formhash = FORMHASH
    return poster_obj


def _seed_news(save_dir, stem, *, title, translated=None, message="正文"):
    (save_dir / f"{stem}.txt").write_text(message, encoding="utf-8")
    meta = {"title": title}
    if translated is not None:
        meta["translated_title"] = translated
    (save_dir / f"{stem}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _write_records(save_dir, records) -> str:
    path = save_dir / ".mcbbs_threads.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _read_records(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _legacy(**overrides):
    record = {
        "tid": 6700, "fid": 45, "url": f"{BASE}/thread-6700-1-1.html",
        "title": "标题", "news_type": "java_snapshot", "color": "#3C9D40",
        "bold": False, "state": "legacy", "posted_at": "", "updated_at": "",
        "attempts": 0, "last_error": "",
    }
    record.update(overrides)
    return record


class TestNewsTypeResolution:
    def test_module_tag_wins_over_title(self):
        assert poster._resolve_news_type(
            "[module_bedrock_beta_header]正文", "Minecraft Snapshot", "快照") == "bedrock_beta"

    def test_falls_back_to_english_then_chinese_then_normal(self):
        assert poster._resolve_news_type("正文", "Minecraft 25w08a Snapshot", "") == "java_snapshot"
        assert poster._resolve_news_type("正文", "Some news", "26 快照 9") == "java_snapshot"
        assert poster._resolve_news_type("正文", "Some news", "某条资讯") == "normal"

    def test_local_type_reads_artifacts(self, tmp_path):
        _seed_news(tmp_path, "news_a", title="Minecraft Beta 26.70.24", message="")
        assert poster._local_news_type(str(tmp_path), "news_a") == "bedrock_beta"

    def test_local_type_without_json_is_normal(self, tmp_path):
        assert poster._local_news_type(str(tmp_path), "missing") == "normal"


class TestScanOutputTitles:
    def test_collects_both_title_forms(self, tmp_path):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 9", translated="Minecraft 26.4 快照 9")
        titles = poster._scan_output_titles(str(tmp_path))
        assert titles["Minecraft 26.4 快照 9 [AI翻译]"] == "news_a"
        assert titles["Minecraft 26.4 快照 9"] == "news_a"

    def test_skips_opus_and_hidden_and_corrupt(self, tmp_path):
        _seed_news(tmp_path, "news_a", title="A")
        (tmp_path / "news_a.opus.json").write_text(json.dumps({"title": "A"}), encoding="utf-8")
        (tmp_path / ".posted_bili.json").write_text('["x"]', encoding="utf-8")
        (tmp_path / "broken.json").write_text("{", encoding="utf-8")
        titles = poster._scan_output_titles(str(tmp_path))
        assert set(titles.values()) == {"news_a"}


class TestIndexHistory:
    def test_records_matched_history_as_legacy(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 9", translated="Minecraft 26.4 快照 9")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads", lambda self, pages=2: [
            poster.MyThread(6816, "Minecraft 26.4 快照 9 [AI翻译]", forum_name="幻翼块讯"),
            poster.MyThread(6815, "和产物对不上的帖子", forum_name="幻翼块讯"),
        ])

        stats = _make_poster().index_history(str(tmp_path))

        assert stats["threads"] == 2
        assert stats["recorded"] == 1
        record = _read_records(tmp_path / ".mcbbs_threads.json")["news_a"]
        assert record["state"] == "legacy"
        assert record["tid"] == 6816
        assert record["news_type"] == "java_snapshot"
        assert record["color"] == "#3C9D40"
        assert record["tid_source"] == "历史登记"

    def test_skips_threads_already_recorded(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 9")
        _write_records(tmp_path, {"news_a": _legacy(tid=6816, state="done")})
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6816, "Minecraft 26.4 快照 9 [AI翻译]")])

        stats = _make_poster().index_history(str(tmp_path))

        assert stats["matched"] == 1
        assert stats["skipped"] == 1
        assert _read_records(tmp_path / ".mcbbs_threads.json")["news_a"]["state"] == "done"

    def test_skips_type_without_color(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_c", title="某条时评", message="[module_commentary_header]正文")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [poster.MyThread(6820, "某条时评 [AI翻译]")])

        stats = _make_poster().index_history(str(tmp_path))

        assert stats["recorded"] == 0
        assert stats["no_color"] == 1
        assert not (tmp_path / ".mcbbs_threads.json").exists()

    def test_skips_struck_threads(self, tmp_path, monkeypatch):
        """标题带删除线 = 帖子已删除/测试稿/撤掉 AI 翻译，不能登记。"""
        _seed_news(tmp_path, "news_a", title="Minecraft 电影建造挑战投票活动")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6700, "Minecraft 电影建造挑战投票活动 [AI翻译]",
                                                recy=True, forum_name="幻翼块讯")])

        stats = _make_poster().index_history(str(tmp_path))

        assert stats["recorded"] == 0
        assert stats["recycled"] == 1
        assert not (tmp_path / ".mcbbs_threads.json").exists()

    def test_skips_excluded_forum_by_name(self, tmp_path, monkeypatch):
        """落在「块讯回收区」的帖子即使标题正常也不能登记。"""
        _seed_news(tmp_path, "news_a", title="Minecraft 电影建造挑战投票活动")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6701, "Minecraft 电影建造挑战投票活动 [AI翻译]",
                                                forum_name="块讯回收区")])

        stats = _make_poster().index_history(str(tmp_path))

        assert stats["recorded"] == 0
        assert stats["excluded"] == 1
        assert not (tmp_path / ".mcbbs_threads.json").exists()

    def test_skips_excluded_forum_by_fid(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 9")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6702, "Minecraft 26.4 快照 9 [AI翻译]",
                                                forum_fid=47)])

        poster_obj = MCBBSPoster({
            "base_url": BASE, "forum_fid": 45, "username": "newspaper", "password": "secret",
            "highlight": {"exclude_forums": [47]},
        })
        stats = poster_obj.index_history(str(tmp_path))

        assert stats["recorded"] == 0
        assert stats["excluded"] == 1

    def test_dry_run_reports_without_writing(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 9")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6703, "Minecraft 26.4 快照 9 [AI翻译]",
                                                forum_name="幻翼块讯")])

        stats = _make_poster().index_history(str(tmp_path), dry_run=True)

        assert stats["recorded"] == 1
        assert not (tmp_path / ".mcbbs_threads.json").exists()


class TestIndexHistorySiteState:
    """历史登记的颜色和加粗必须取自版块列表的现状，不能写死 false。"""

    def test_uses_site_colour_and_bold(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 8")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6773, "Minecraft 26.4 快照 8 [AI翻译]",
                                                forum_name="幻翼块讯")])
        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads",
                            lambda self, pages=3: [
                                poster.ForumThread(6773, "Minecraft 26.4 快照 8 [AI翻译]",
                                                   typeid=4, type_name="Java版资讯",
                                                   color="#3C9D40", bold=True)])

        stats = _make_poster().index_history(str(tmp_path))

        record = _read_records(tmp_path / ".mcbbs_threads.json")["news_a"]
        assert record["color"] == "#3C9D40"
        assert record["bold"] is True
        assert record["typeid"] == 4
        assert stats["site_bold"] == 1

    def test_forum_list_failure_falls_back_to_config(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 8")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6773, "Minecraft 26.4 快照 8 [AI翻译]")])

        def boom(self, pages=3):
            raise poster.requests.ConnectionError("boom")

        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads", boom)

        _make_poster().index_history(str(tmp_path))

        record = _read_records(tmp_path / ".mcbbs_threads.json")["news_a"]
        assert record["color"] == "#3C9D40"   # 配置里的默认色
        assert record["bold"] is False

    def test_unhighlighted_thread_keeps_config_colour(self, tmp_path, monkeypatch):
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 8")
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6773, "Minecraft 26.4 快照 8 [AI翻译]")])
        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads",
                            lambda self, pages=3: [
                                poster.ForumThread(6773, "Minecraft 26.4 快照 8 [AI翻译]",
                                                   typeid=4)])

        _make_poster().index_history(str(tmp_path))

        record = _read_records(tmp_path / ".mcbbs_threads.json")["news_a"]
        assert record["color"] == "#3C9D40"
        assert record["bold"] is False

    def test_refreshes_stale_legacy_bold(self, tmp_path, monkeypatch):
        """早先版本把 legacy 的 bold 写死 false，重跑要按站点现状纠正。"""
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 8")
        _write_records(tmp_path, {"news_a": _legacy(tid=6773, bold=False, color="#3C9D40")})
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6773, "Minecraft 26.4 快照 8 [AI翻译]")])
        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads",
                            lambda self, pages=3: [
                                poster.ForumThread(6773, "Minecraft 26.4 快照 8 [AI翻译]",
                                                   typeid=4, color="#3C9D40", bold=True)])

        stats = _make_poster().index_history(str(tmp_path))

        assert stats["refreshed"] == 1
        record = _read_records(tmp_path / ".mcbbs_threads.json")["news_a"]
        assert record["bold"] is True
        assert record["state"] == "legacy"

    def test_refresh_does_not_touch_pending_records(self, tmp_path, monkeypatch):
        """pending/done 是我们自己提交的状态，不能被历史刷新覆盖。"""
        _seed_news(tmp_path, "news_a", title="Minecraft 26.4 快照 8")
        _write_records(tmp_path, {"news_a": _legacy(tid=6773, state="pending", bold=True)})
        monkeypatch.setattr(MCBBSPoster, "fetch_my_threads",
                            lambda self, pages=2: [
                                poster.MyThread(6773, "Minecraft 26.4 快照 8 [AI翻译]")])
        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads",
                            lambda self, pages=3: [
                                poster.ForumThread(6773, "Minecraft 26.4 快照 8 [AI翻译]",
                                                   typeid=4, bold=False)])

        stats = _make_poster().index_history(str(tmp_path))

        assert stats["refreshed"] == 0
        assert _read_records(tmp_path / ".mcbbs_threads.json")["news_a"]["bold"] is True


class TestPromoteLegacy:
    def test_promotes_newest_limit(self, tmp_path):
        path = _write_records(tmp_path, {
            "news_1": _legacy(tid=6701),
            "news_2": _legacy(tid=6702),
            "news_3": _legacy(tid=6703),
        })

        promoted = _make_poster().promote_legacy(path, 2)

        assert promoted == 2
        records = _read_records(path)
        assert records["news_3"]["state"] == "pending"
        assert records["news_2"]["state"] == "pending"
        assert records["news_1"]["state"] == "legacy"

    def test_resets_attempts(self, tmp_path):
        path = _write_records(tmp_path, {"news_1": _legacy(tid=6701, attempts=4, last_error="旧错误")})

        _make_poster().promote_legacy(path, 1)

        record = _read_records(path)["news_1"]
        assert record["attempts"] == 0
        assert record["last_error"] == ""

    def test_skips_missing_color(self, tmp_path):
        path = _write_records(tmp_path, {"news_1": _legacy(tid=6701, color="")})

        assert _make_poster().promote_legacy(path, 1) == 0
        assert _read_records(path)["news_1"]["state"] == "legacy"


class TestHighlightStatus:
    """--highlight-status：把记录和站点现状摆在一起，给「到底生效了没」用。"""

    def test_reports_match_and_mismatch(self, tmp_path, monkeypatch):
        _write_records(tmp_path, {
            "news_a": _legacy(tid=6808, color="#3C9D40", bold=True),
            "news_b": _legacy(tid=6773, color="#3C9D40", bold=True),
        })
        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads",
                            lambda self, pages=3: [
                                poster.ForumThread(6808, "a", typeid=4,
                                                   color="#3C9D40", bold=True),
                                poster.ForumThread(6773, "b", typeid=4,
                                                   color="#3C9D40", bold=False)])

        stats = _make_poster().highlight_status(str(tmp_path / ".mcbbs_threads.json"))

        assert stats["checked"] == 2
        assert stats["same"] == 1
        assert stats["different"] == 1
        assert stats["missing"] == 0

    def test_missing_thread_is_counted(self, tmp_path, monkeypatch):
        _write_records(tmp_path, {"news_a": _legacy(tid=6808, color="#3C9D40")})
        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads",
                            lambda self, pages=3: [])

        stats = _make_poster().highlight_status(str(tmp_path / ".mcbbs_threads.json"))

        assert stats["missing"] == 1

    def test_empty_records(self, tmp_path):
        stats = _make_poster().highlight_status(str(tmp_path / ".mcbbs_threads.json"))
        assert stats == {"checked": 0, "same": 0, "different": 0, "missing": 0}

    def test_forum_list_failure_is_reported(self, tmp_path, monkeypatch):
        _write_records(tmp_path, {"news_a": _legacy(tid=6808)})

        def boom(self, pages=3):
            raise poster.requests.ConnectionError("boom")

        monkeypatch.setattr(MCBBSPoster, "fetch_forum_threads", boom)

        stats = _make_poster().highlight_status(str(tmp_path / ".mcbbs_threads.json"))

        assert stats["checked"] == 0


class TestMainDispatch:
    """高亮相关的开关必须走补高亮入口，不能掉进「发帖」那条路。"""

    def _run(self, monkeypatch, tmp_path, *flags):
        calls = []
        monkeypatch.setattr(sys, "argv", ["poster.py", "--dir", str(tmp_path), *flags])
        # 账号留空：一旦走错到发帖路径会以「未配置账号密码」退出 1，
        # 和补高亮入口的 0 区分得开
        monkeypatch.setattr(poster, "load_poster_config", lambda *a, **k: {
            "base_url": "https://www.mcbbs.co", "forum_fid": 45,
            "username": "", "password": "", "sortid_map": {},
        })
        monkeypatch.setattr(poster, "_run_highlight_cli",
                            lambda *args: calls.append(args) or 0)
        with pytest.raises(SystemExit) as exit_info:
            poster.main()
        return exit_info.value.code, calls

    @pytest.mark.parametrize("flag", ["--reconcile-bold", "--highlight-status",
                                      "--highlight-only", "--index-history"])
    def test_highlight_flags_go_to_highlight_cli(self, monkeypatch, tmp_path, flag):
        code, calls = self._run(monkeypatch, tmp_path, flag)
        assert code == 0
        assert len(calls) == 1


class TestHighlightCli:
    def _args(self, **overrides):
        base = {"index_history": False, "highlight_legacy": None, "highlight_stem": None,
                "highlight_tid": None, "color": None, "highlight_only": True,
                "highlight_status": False, "reconcile_bold": False, "bold": False,
                "no_bold": False, "dry_run": False}
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_dry_run_previews_without_network(self, tmp_path, monkeypatch):
        calls = []

        class FakePoster:
            def __init__(self, config):
                pass

            def process_pending_highlights(self, records_path):
                calls.append(records_path)

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        _write_records(tmp_path, {"news_a": _legacy(state="pending")})

        code = poster._run_highlight_cli(self._args(dry_run=True),
                                         {"username": "u", "password": "p"},
                                         str(tmp_path), str(tmp_path / ".mcbbs_threads.json"))

        assert code == 0
        assert calls == []

    def test_sweep_runs_when_not_dry_run(self, tmp_path, monkeypatch):
        calls = []

        class FakePoster:
            def __init__(self, config):
                pass

            def process_pending_highlights(self, records_path):
                calls.append(records_path)

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        records_path = str(tmp_path / ".mcbbs_threads.json")
        _write_records(tmp_path, {"news_a": _legacy(state="pending")})

        code = poster._run_highlight_cli(self._args(),
                                         {"username": "u", "password": "p"},
                                         str(tmp_path), records_path)

        assert code == 0
        assert calls == [records_path]

    def test_missing_news_dir_is_error(self, tmp_path):
        code = poster._run_highlight_cli(self._args(), {"username": "u", "password": "p"},
                                         str(tmp_path / "nope"), str(tmp_path / ".mcbbs_threads.json"))
        assert code == 1

    def test_index_history_requires_credentials(self, tmp_path):
        _seed_news(tmp_path, "news_a", title="A")
        code = poster._run_highlight_cli(self._args(index_history=True, dry_run=False),
                                         {"username": "", "password": ""},
                                         str(tmp_path), str(tmp_path / ".mcbbs_threads.json"))
        assert code == 1

    def test_highlight_status_needs_no_login(self, tmp_path, monkeypatch):
        called = []

        class FakePoster:
            def __init__(self, config):
                pass

            def highlight_status(self, records_path, pages=3):
                called.append(records_path)
                return {"checked": 3, "same": 3, "different": 0, "missing": 0}

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        code = poster._run_highlight_cli(self._args(highlight_only=False, highlight_status=True),
                                         {"username": "", "password": ""},
                                         str(tmp_path), str(tmp_path / ".mcbbs_threads.json"))

        assert code == 0
        assert called == [str(tmp_path / ".mcbbs_threads.json")]

    def test_highlight_status_fails_when_site_unreachable(self, tmp_path, monkeypatch):
        """读不到版块列表时什么都没查，不能报成功。"""

        class FakePoster:
            def __init__(self, config):
                pass

            def highlight_status(self, records_path, pages=3):
                return {"checked": 0, "same": 0, "different": 0, "missing": 0}

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        code = poster._run_highlight_cli(self._args(highlight_only=False, highlight_status=True),
                                         {"username": "", "password": ""},
                                         str(tmp_path), str(tmp_path / ".mcbbs_threads.json"))

        assert code == 1

    def test_highlight_stem_marks_one_record(self, tmp_path, monkeypatch):
        """单篇验证：--highlight-stem 只把这一条改成 pending，并交给 only= 单篇处理。"""
        calls = []

        class FakePoster:
            def __init__(self, config):
                pass

            def _save_records(self, records_path, records):
                poster.save_thread_records(records_path, records)

            def process_pending_highlights(self, records_path, only=None):
                calls.append((records_path, only))
                return {"pending": 1, "highlighted": 1, "failed": 0}

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        records_path = str(tmp_path / ".mcbbs_threads.json")
        _write_records(tmp_path, {
            "news_a": _legacy(tid=6808, state="legacy", bold=False),
            "news_b": _legacy(tid=6773, state="legacy"),
        })

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_stem="news_a", bold=True),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 0
        records = _read_records(records_path)
        assert records["news_a"]["state"] == "pending"
        assert records["news_a"]["bold"] is True
        assert records["news_b"]["state"] == "legacy"
        # 只处理这一条：别的 pending 不跟着被提交
        assert calls == [(records_path, "news_a")]

    def test_highlight_stem_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "MCBBSPoster", lambda config: type(
            "P", (), {"process_pending_highlights": lambda self, path: {}})())
        records_path = str(tmp_path / ".mcbbs_threads.json")
        _write_records(tmp_path, {"news_a": _legacy(tid=6808, state="legacy")})

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_stem="news_a", dry_run=True),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 0
        assert _read_records(records_path)["news_a"]["state"] == "legacy"

    def test_highlight_tid_requires_colour(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "MCBBSPoster", lambda config: type("P", (), {})())
        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_tid=6816),
            {"username": "u", "password": "p"},
            str(tmp_path), str(tmp_path / ".mcbbs_threads.json"))
        assert code == 1

    def test_highlight_tid_creates_manual_record(self, tmp_path, monkeypatch):
        """手工指定的帖子不依赖 output 产物，键名用 tid_<n>。"""
        calls = []

        class FakePoster:
            forum_fid = 45
            base_url = BASE

            def __init__(self, config):
                pass

            def _save_records(self, records_path, records):
                poster.save_thread_records(records_path, records)

            def fetch_forum_threads(self, pages=3):
                return [poster.ForumThread(6816, "TwitchCon 揭晓 2027 年首个游戏更新",
                                           typeid=16, color="", bold=False,
                                           posted="2026-11-18")]

            def process_pending_highlights(self, records_path, only=None):
                calls.append((records_path, only))
                return {"pending": 1, "highlighted": 1, "failed": 0}

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        records_path = str(tmp_path / ".mcbbs_threads.json")

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_tid=6816, color="#EC1282"),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 0
        record = _read_records(records_path)["tid_6816"]
        assert record["tid"] == 6816
        assert record["color"] == "#EC1282"
        assert record["bold"] is False
        assert record["typeid"] == 16
        # 年龄按帖子真实的发帖日期算，不是「登记时间」
        assert record["posted_at"] == "2026-11-18T00:00:00"
        assert record["state"] == "pending"
        assert calls == [(records_path, "tid_6816")]

    def test_highlight_tid_keeps_existing_bold_without_override(self, tmp_path, monkeypatch):
        """没给 --bold/--no-bold 时沿用站上现状，别把人工加粗的帖子取消加粗。"""
        class FakePoster:
            forum_fid = 45
            base_url = BASE

            def __init__(self, config):
                pass

            def _save_records(self, records_path, records):
                poster.save_thread_records(records_path, records)

            def fetch_forum_threads(self, pages=3):
                return [poster.ForumThread(6772, "新的观看时长挑战 [AI翻译]",
                                           typeid=16, color="#2B65B7", bold=True)]

            def process_pending_highlights(self, records_path, only=None):
                return {"pending": 1, "highlighted": 1, "failed": 0}

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        records_path = str(tmp_path / ".mcbbs_threads.json")

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_tid=6772, color="#2B65B7"),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 0
        assert _read_records(records_path)["tid_6772"]["bold"] is True

    def test_highlight_tid_reuses_record_of_same_tid(self, tmp_path, monkeypatch):
        """同一篇帖子已经按 stem 登记过，就复用它，别再建一条 tid_<n> 重复记录。"""
        class FakePoster:
            forum_fid = 45
            base_url = BASE

            def __init__(self, config):
                pass

            def _save_records(self, records_path, records):
                poster.save_thread_records(records_path, records)

            def fetch_forum_threads(self, pages=3):
                return [poster.ForumThread(6816, "TwitchCon 揭晓 2027 年首个游戏更新",
                                           typeid=16, color="", bold=False)]

            def process_pending_highlights(self, records_path, only=None):
                return {"pending": 1, "highlighted": 1, "failed": 0}

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        records_path = str(tmp_path / ".mcbbs_threads.json")
        _write_records(tmp_path, {"news_twitchcon": _legacy(tid=6816, state="legacy")})

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_tid=6816, color="#EC1282"),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 0
        records = _read_records(records_path)
        assert set(records) == {"news_twitchcon"}
        assert records["news_twitchcon"]["state"] == "pending"

    def test_highlight_tid_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        class FakePoster:
            forum_fid = 45
            base_url = BASE

            def __init__(self, config):
                pass

            def fetch_forum_threads(self, pages=3):
                return []

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        records_path = str(tmp_path / ".mcbbs_threads.json")

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_tid=6816, color="#EC1282",
                       bold=True, dry_run=True),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 0
        assert not (tmp_path / ".mcbbs_threads.json").exists()

    def test_highlight_stem_unknown_reports_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poster, "MCBBSPoster", lambda config: type("P", (), {})())
        records_path = str(tmp_path / ".mcbbs_threads.json")
        _write_records(tmp_path, {"news_a": _legacy()})

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_stem="nope"),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 1

    @pytest.mark.parametrize("outcome", [
        {"stale": 1},      # 超过 max_age_days，不再处理
        {"gave_up": 1},    # 等审核超时 / 标题找不到
        {"dropped": 1},    # 帖子落在 exclude_forums 里
    ])
    def test_highlight_stem_not_applied_exits_nonzero(self, tmp_path, monkeypatch, outcome):
        """单篇验证没真设上时不能返回 0：stale/gave_up/dropped 都不计入 failed。"""
        class FakePoster:
            def __init__(self, config):
                pass

            def _save_records(self, records_path, records):
                poster.save_thread_records(records_path, records)

            def process_pending_highlights(self, records_path, only=None):
                return {"pending": 1, "highlighted": 0, "failed": 0, **outcome}

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        records_path = str(tmp_path / ".mcbbs_threads.json")
        _write_records(tmp_path, {"news_a": _legacy(tid=6808, state="legacy")})

        code = poster._run_highlight_cli(
            self._args(highlight_only=False, highlight_stem="news_a", bold=True),
            {"username": "u", "password": "p"}, str(tmp_path), records_path)

        assert code == 1
