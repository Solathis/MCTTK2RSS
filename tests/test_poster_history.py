"""test_poster_history.py — 历史帖登记（legacy）、限量提升与 CLI 补高亮入口"""
import argparse
import json

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


class TestHighlightCli:
    def _args(self, **overrides):
        base = {"index_history": False, "highlight_legacy": None,
                "highlight_only": True, "dry_run": False}
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
