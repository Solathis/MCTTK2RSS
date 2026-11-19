"""test_poster_retention.py — 记录生命周期：过期不再处理、自动清理、无变化不写盘"""
import json
import time

import pytest

import poster
from poster import MCBBSPoster

BASE = "https://www.mcbbs.co"
FORMHASH = "abc12345"
OK_RESPONSE = "<script>succeedhandle_mods('u',{},{});</script>"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(poster.time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _no_forum_scan(monkeypatch):
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


class QuietSession:
    """只记账、不回应的假会话：把这些用例跟真实请求彻底隔开。"""

    def __init__(self):
        self.headers = {}
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, **kwargs):
        return FakeResponse("<html></html>")

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(OK_RESPONSE)


def _days_ago(days: float) -> str:
    return time.strftime(poster.TIMESTAMP_FORMAT, time.localtime(time.time() - days * 86400))


def _record(**overrides):
    record = {
        "tid": 6808,
        "fid": 45,
        "url": f"{BASE}/thread-6808-1-1.html",
        "title": "Minecraft 26.4 快照 9 [AI翻译]",
        "news_type": "java_snapshot",
        "typeid": 4,
        "color": "#3C9D40",
        "bold": False,
        "state": "pending",
        "posted_at": _days_ago(1),
        "registered_at": _days_ago(1),
        "updated_at": _days_ago(1),
        "attempts": 0,
        "last_error": "",
    }
    record.update(overrides)
    return record


def _write_records(tmp_path, records) -> str:
    path = tmp_path / ".mcbbs_threads.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _read_records(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _make_poster(highlight=None) -> MCBBSPoster:
    poster_obj = MCBBSPoster({
        "base_url": BASE, "forum_fid": 45, "username": "newspaper", "password": "secret",
        "sortid_map": {"java_snapshot": 4, "java_release": 4, "bedrock_beta": 20,
                       "bedrock_release": 20, "normal": 16},
        "highlight": highlight or {},
    })
    poster_obj.session = QuietSession()
    poster_obj.formhash = FORMHASH
    return poster_obj


class TestRecordTime:
    """年龄必须从「发帖/登记」算，不能从 updated_at 算 —— 否则永远不会过期。"""

    def test_prefers_posted_at(self):
        record = _record(posted_at=_days_ago(10), registered_at=_days_ago(1))
        started = poster._record_time(record)
        assert started == pytest.approx(time.time() - 10 * 86400, abs=120)

    def test_falls_back_to_registered_at(self):
        record = _record(posted_at="", registered_at=_days_ago(5))
        assert poster._record_time(record) == pytest.approx(time.time() - 5 * 86400, abs=120)

    def test_ignores_updated_at(self):
        record = _record(posted_at="", registered_at="", updated_at=_days_ago(0))
        assert poster._record_time(record) is None


class TestTooOld:
    def test_over_limit(self):
        poster_obj = _make_poster({"max_age_days": 30})
        assert poster_obj._too_old(_record(posted_at=_days_ago(31)), time.time()) is True

    def test_negative_limit_is_rejected(self):
        """负数会让窗口变成「立刻过期」，合并配置时直接丢掉。"""
        poster_obj = _make_poster({"max_age_days": -5})
        assert poster_obj._max_age_days() == poster.DEFAULT_HIGHLIGHT["max_age_days"]

    def test_promote_restarts_the_waiting_clock(self, tmp_path):
        """提升一条老 legacy 记录要重置「等审核」的起算点，否则立刻判超时。"""
        poster_obj = _make_poster({"max_age_days": 30, "max_age_hours": 168})
        path = _write_records(tmp_path, {"news_a": _record(
            state="legacy", posted_at=_days_ago(20), registered_at=_days_ago(20))})

        poster_obj.promote_legacy(path, 1)

        record = _read_records(path)["news_a"]
        assert record["state"] == "pending"
        assert poster_obj._expired(record, time.time()) is False
        # 但「帖子太老」这条判据仍按真实的发帖时间算
        assert poster_obj._too_old(record, time.time()) is False

    def test_within_limit(self):
        poster_obj = _make_poster({"max_age_days": 30})
        assert poster_obj._too_old(_record(posted_at=_days_ago(29)), time.time()) is False

    def test_unknown_age_is_never_too_old(self):
        poster_obj = _make_poster({"max_age_days": 30})
        assert poster_obj._too_old(_record(posted_at="", registered_at=""), time.time()) is False

    def test_zero_days_disables_the_gate(self):
        poster_obj = _make_poster({"max_age_days": 0})
        assert poster_obj._too_old(_record(posted_at=_days_ago(999)), time.time()) is False


class TestSweepStopsAtStaleRecords:
    """卡在审核里的帖子不能永远每轮去探测一次。"""

    def test_stale_pending_is_given_up_without_probing(self, tmp_path, monkeypatch):
        def no_probe(*_args, **_kwargs):
            raise AssertionError("过期记录不该再被探测")

        monkeypatch.setattr(poster, "probe_thread", no_probe)
        path = _write_records(tmp_path, {
            "news_old": _record(tid=6809, posted_at=_days_ago(40), registered_at=_days_ago(40)),
        })
        poster_obj = _make_poster({"max_age_days": 30})

        stats = poster_obj.process_pending_highlights(path)

        assert stats["stale"] == 1
        assert stats["highlighted"] == 0
        # 过期就判放弃，并在同一次落盘时被清理掉
        assert "news_old" not in _read_records(path)

    def test_fresh_pending_still_probed(self, tmp_path, monkeypatch):
        calls = []

        def probe(base, tid, timeout=poster.LOGIN_TIMEOUT):
            calls.append(tid)
            return poster.ThreadProbe(False, forum_fid=45, crumb="幻翼块讯")

        monkeypatch.setattr(poster, "probe_thread", probe)
        path = _write_records(tmp_path, {"news_new": _record(tid=6816)})
        poster_obj = _make_poster({"max_age_days": 30})

        stats = poster_obj.process_pending_highlights(path)

        assert calls == [6816]
        assert stats["stale"] == 0
        assert _read_records(path)["news_new"]["last_error"] == "仍在审核中"

    def test_waiting_too_long_gives_up_without_probing(self, tmp_path, monkeypatch):
        """超过 max_age_hours 还没过审：不再每轮去探测。"""
        def no_probe(*_args, **_kwargs):
            raise AssertionError("已经等超时的记录不该再探测")

        monkeypatch.setattr(poster, "probe_thread", no_probe)
        path = _write_records(tmp_path, {
            "news_slow": _record(tid=6816, posted_at=_days_ago(10),
                                 registered_at=_days_ago(10), last_error="仍在审核中"),
        })
        poster_obj = _make_poster({"max_age_days": 30, "max_age_hours": 168})

        stats = poster_obj.process_pending_highlights(path)

        assert stats["gave_up"] == 1
        assert _read_records(path)["news_slow"]["state"] == "gave_up"


class TestPrune:
    def test_drops_expired_records_keeps_pending_and_bold(self, tmp_path):
        records = {
            "old_legacy": _record(state="legacy", posted_at=_days_ago(40),
                                  registered_at=_days_ago(40)),
            "old_done": _record(state="done", posted_at=_days_ago(40),
                                registered_at=_days_ago(40)),
            # 真上过色：highlighted_at 是成功提交时才写的，留着才能随时取消加粗
            "old_bold": _record(state="done", bold=True, posted_at=_days_ago(400),
                                registered_at=_days_ago(400),
                                highlighted_at=_days_ago(400)),
            # 只登记过、从没提交成功：bold 只是意图，不该让它永久留在账本里
            "old_gave_up": _record(state="gave_up", bold=True, posted_at=_days_ago(400),
                                   registered_at=_days_ago(400)),
            "old_pending": _record(state="pending", posted_at=_days_ago(40),
                                   registered_at=_days_ago(40)),
            "fresh_done": _record(state="done", posted_at=_days_ago(3),
                                  registered_at=_days_ago(3)),
        }
        poster_obj = _make_poster({"max_age_days": 30})

        dropped = poster_obj._prune_records(records)

        assert dropped == 3
        assert set(records) == {"old_bold", "old_pending", "fresh_done"}

    def test_retired_pending_record_is_not_kept_forever(self):
        """pending + bold 被判 gave_up 后一直留在记录里，是这条规则曾经的漏网之鱼。"""
        records = {"news_x": _record(state="gave_up", bold=True,
                                     posted_at=_days_ago(40), registered_at=_days_ago(40))}
        poster_obj = _make_poster({"max_age_days": 30})

        assert poster_obj._prune_records(records) == 1
        assert records == {}

    def test_stamps_registered_at_for_legacy_records(self):
        """老记录没有 registered_at，补一次作为计时起点，不直接删。"""
        records = {"news_a": _record(state="legacy", posted_at="", registered_at="")}
        poster_obj = _make_poster({"max_age_days": 30})

        dropped = poster_obj._prune_records(records)

        assert dropped == 0
        assert records["news_a"]["registered_at"]

    def test_save_prunes_before_writing(self, tmp_path):
        path = _write_records(tmp_path, {})
        records = {
            "old_legacy": _record(state="legacy", posted_at=_days_ago(40),
                                  registered_at=_days_ago(40)),
            "keep": _record(state="done", posted_at=_days_ago(2), registered_at=_days_ago(2)),
        }
        poster_obj = _make_poster({"max_age_days": 30})

        poster_obj._save_records(path, records)

        assert set(_read_records(path)) == {"keep"}


class TestNoOpWrites:
    """每小时一轮，没变化就不该重写文件（容器里每次写盘都是多余的抖）。"""

    def _run(self, tmp_path, monkeypatch, record):
        path = _write_records(tmp_path, {"news_x": record})
        writes = []
        monkeypatch.setattr(poster, "save_thread_records",
                            lambda *args: writes.append(args))
        monkeypatch.setattr(poster, "probe_thread",
                            lambda *a, **k: poster.ThreadProbe(False, forum_fid=45, crumb="幻翼块讯"))
        stats = _make_poster({"max_age_days": 30}).process_pending_highlights(path)
        return stats, writes

    def test_repeated_sweep_without_changes_writes_nothing(self, tmp_path, monkeypatch):
        stats, writes = self._run(tmp_path, monkeypatch,
                                 _record(tid=6816, last_error="仍在审核中"))
        assert stats["pending"] == 1
        assert writes == []

    def test_changed_sweep_writes_once(self, tmp_path, monkeypatch):
        stats, writes = self._run(tmp_path, monkeypatch, _record(tid=6816, last_error=""))
        assert stats["pending"] == 1
        assert len(writes) == 1


class TestIndexHistoryAgeGate:
    """太久以前的产物不登记，否则 .mcbbs_threads.json 会塞满永远用不上的老记录。"""

    def _poster(self, tmp_path, release_date, title="Minecraft 26.4 快照 9 [AI翻译]"):
        artifact = tmp_path / "news_a.json"
        artifact.write_text(json.dumps({
            "title": "Minecraft 26.4 Snapshot 9",
            "translated_title": title,
            "release_date": release_date,
        }, ensure_ascii=False), encoding="utf-8")
        poster_obj = _make_poster({"max_age_days": 30})
        poster_obj.fetch_my_threads = lambda pages=15: [
            poster.MyThread(tid=6808, title=title, forum_fid=45, forum_name="幻翼块讯"),
        ]
        poster_obj.fetch_forum_threads = lambda pages=3: []
        return poster_obj

    def test_skips_artifact_older_than_limit(self, tmp_path):
        poster_obj = self._poster(tmp_path, "2026-05-05T15:34:00Z")
        stats = poster_obj.index_history(str(tmp_path))
        assert stats["expired"] == 1
        assert stats["recorded"] == 0

    def test_registers_recent_artifact(self, tmp_path):
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        poster_obj = self._poster(tmp_path, recent)
        stats = poster_obj.index_history(str(tmp_path))
        assert stats["expired"] == 0
        assert stats["recorded"] == 1
        record = _read_records(str(tmp_path / ".mcbbs_threads.json"))["news_a"]
        assert record["registered_at"]

    def test_unknown_date_is_not_gated(self, tmp_path):
        poster_obj = self._poster(tmp_path, "")
        stats = poster_obj.index_history(str(tmp_path))
        assert stats["expired"] == 0
        assert stats["recorded"] == 1

    def test_cleans_up_stale_legacy_records(self, tmp_path):
        """已经登记过的老 legacy 记录也跟着产物日期一起清掉。"""
        poster_obj = self._poster(tmp_path, "2026-05-05T15:34:00Z")
        _write_records(tmp_path, {"news_a": _record(state="legacy", posted_at="",
                                                    registered_at=_days_ago(1))})

        stats = poster_obj.index_history(str(tmp_path))

        assert stats["expired"] == 1
        assert stats["pruned"] == 1
        assert _read_records(str(tmp_path / ".mcbbs_threads.json")) == {}

    def test_keeps_bold_legacy_records(self, tmp_path):
        """加粗中的记录必须留着，否则以后没法取消加粗。"""
        poster_obj = self._poster(tmp_path, "2026-05-05T15:34:00Z")
        _write_records(tmp_path, {"news_a": _record(state="legacy", bold=True,
                                                    posted_at="", registered_at=_days_ago(1))})

        poster_obj.index_history(str(tmp_path))

        assert "news_a" in _read_records(str(tmp_path / ".mcbbs_threads.json"))

    def test_dry_run_does_not_prune_or_write(self, tmp_path, monkeypatch):
        poster_obj = self._poster(tmp_path, "2026-05-05T15:34:00Z")
        _write_records(tmp_path, {"news_a": _record(state="legacy", posted_at="",
                                                    registered_at=_days_ago(1))})
        writes = []
        monkeypatch.setattr(poster, "save_thread_records", lambda *a: writes.append(a))

        stats = poster_obj.index_history(str(tmp_path), dry_run=True)

        assert stats["pruned"] == 0
        assert writes == []
        assert "news_a" in _read_records(str(tmp_path / ".mcbbs_threads.json"))

    def test_reposted_old_article_is_kept_while_on_the_board(self, tmp_path):
        """旧文章被重新发一遍：产物日期老，但帖子是新的，仍然要登记。"""
        poster_obj = self._poster(tmp_path, "2026-05-05T15:34:00Z")
        poster_obj.fetch_forum_threads = lambda pages=3: [
            poster.ForumThread(tid=6808, title="Minecraft 26.4 快照 9 [AI翻译]",
                               typeid=4, color="#3C9D40", bold=False,
                               posted=time.strftime("%Y-%m-%d")),
        ]

        stats = poster_obj.index_history(str(tmp_path))

        assert stats["expired"] == 0
        assert stats["recorded"] == 1

    def test_old_thread_is_skipped_even_with_recent_artifact(self, tmp_path):
        """判断以帖子日期为准：帖子本身已经很老就不再登记。"""
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        poster_obj = self._poster(tmp_path, recent)
        poster_obj.fetch_forum_threads = lambda pages=3: [
            poster.ForumThread(tid=6808, title="Minecraft 26.4 快照 9 [AI翻译]",
                               typeid=4, color="#3C9D40", bold=False,
                               posted="2026-01-01"),
        ]

        stats = poster_obj.index_history(str(tmp_path))

        assert stats["expired"] == 1
        assert stats["recorded"] == 0

    def test_prune_keeps_old_artifact_whose_thread_is_new(self, tmp_path):
        poster_obj = self._poster(tmp_path, "2026-05-05T15:34:00Z")
        _write_records(tmp_path, {"news_a": _record(tid=6808, state="legacy", posted_at="",
                                                    registered_at=_days_ago(1))})
        poster_obj.fetch_forum_threads = lambda pages=3: [
            poster.ForumThread(tid=6808, title="Minecraft 26.4 快照 9 [AI翻译]",
                               typeid=4, color="#3C9D40", bold=False,
                               posted=time.strftime("%Y-%m-%d")),
        ]

        poster_obj.index_history(str(tmp_path))

        assert "news_a" in _read_records(str(tmp_path / ".mcbbs_threads.json"))


    def test_posts_real_date_into_posted_at(self, tmp_path):
        """登记时把版块列表上的真实发帖日期写进 posted_at，别让老帖按登记时间算年龄。"""
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        poster_obj = self._poster(tmp_path, recent)
        # 日期相对当前时间取一天前：写死日期过 30 天就会「自己超龄」，用例跟着变红
        posted_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        poster_obj.fetch_forum_threads = lambda pages=3: [
            poster.ForumThread(tid=6808, title="Minecraft 26.4 快照 9 [AI翻译]",
                               typeid=4, color="#3C9D40", bold=False, posted=posted_day),
        ]

        poster_obj.index_history(str(tmp_path))

        record = _read_records(str(tmp_path / ".mcbbs_threads.json"))["news_a"]
        assert record["posted_at"] == f"{posted_day}T00:00:00"

    def test_struck_thread_invalidates_existing_record(self, tmp_path):
        """帖子被打了删除线（已删/测试稿），已有的 legacy 登记也要作废。"""
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        poster_obj = self._poster(tmp_path, recent)
        _write_records(tmp_path, {"news_a": _record(state="legacy", posted_at="",
                                                    registered_at=_days_ago(1))})
        poster_obj.fetch_my_threads = lambda pages=15: [
            poster.MyThread(tid=6808, title="Minecraft 26.4 快照 9 [AI翻译]",
                            recy=True, forum_fid=45, forum_name="幻翼块讯"),
        ]

        stats = poster_obj.index_history(str(tmp_path))

        assert stats["recycled"] == 1
        assert "news_a" not in _read_records(str(tmp_path / ".mcbbs_threads.json"))

    def test_refresh_fills_missing_typeid(self, tmp_path):
        """老记录没有 typeid 就参与不了「同组只留最新一篇加粗」，刷新时补上。"""
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        poster_obj = self._poster(tmp_path, recent)
        _write_records(tmp_path, {"news_a": _record(state="legacy", typeid=None,
                                                    posted_at="", registered_at=_days_ago(1))})
        poster_obj.fetch_forum_threads = lambda pages=3: [
            poster.ForumThread(tid=6808, title="Minecraft 26.4 快照 9 [AI翻译]",
                               typeid=4, color="#3C9D40", bold=False,
                               posted=time.strftime("%Y-%m-%d")),
        ]

        stats = poster_obj.index_history(str(tmp_path))

        assert stats["refreshed"] == 1
        assert _read_records(str(tmp_path / ".mcbbs_threads.json"))["news_a"]["typeid"] == 4


class TestResolveTids:
    """补帖子 ID：查不到可以忍，查到但对不上才算「找不到这篇帖子」。"""

    def test_login_failure_does_not_count_attempt(self):
        poster_obj = _make_poster({"max_age_days": 30})
        poster_obj.session = None

        def boom():
            raise poster.LoginRejected("验证码过不去")

        poster_obj.login = boom
        records = {"news_a": _record(tid=None, title="找不到的标题", attempts=0)}

        poster_obj._resolve_tids(records, dict(records), time.time())

        assert records["news_a"]["attempts"] == 0
        assert records["news_a"]["state"] == "pending"

    def test_excluded_forum_rows_are_not_used(self):
        poster_obj = _make_poster({"max_age_days": 30, "exclude_forums": ["块讯回收区"]})
        poster_obj.session = QuietSession()
        poster_obj.fetch_my_threads = lambda pages=2: [
            poster.MyThread(tid=6816, title="TwitchCon 揭晓 [AI翻译]", recy=False,
                            forum_fid=45, forum_name="块讯回收区"),
        ]
        records = {"news_a": _record(tid=None, title="TwitchCon 揭晓 [AI翻译]")}

        poster_obj._resolve_tids(records, dict(records), time.time())

        assert not records["news_a"].get("tid")

    def test_fallback_only_when_single_missing(self):
        """多条待补时不猜「列表里那个新帖」是谁。"""
        poster_obj = _make_poster({"max_age_days": 30})
        poster_obj.session = QuietSession()
        poster_obj.fetch_my_threads = lambda pages=2: [
            poster.MyThread(tid=6816, title="完全对不上的标题", forum_fid=45),
        ]
        records = {
            "news_a": _record(tid=None, title="甲"),
            "news_b": _record(tid=None, title="乙"),
        }

        poster_obj._resolve_tids(records, dict(records), time.time())

        assert all(not rec.get("tid") for rec in records.values())


class TestRegisterHighlight:
    def test_repost_starts_fresh(self, tmp_path):
        """同一个 stem 又发了一次：旧的 tid / 年龄不能沿用到新帖上。"""
        poster_obj = _make_poster({"max_age_days": 30})
        _write_records(tmp_path, {"news_a": _record(
            tid=6000, posted_at=_days_ago(90), registered_at=_days_ago(90), attempts=4)})

        poster_obj._register_highlight(
            "news_a", str(tmp_path),
            poster.PostResult(url=f"{BASE}/thread-6816-1-1.html", tid=6816),
            "normal", "新帖")

        record = _read_records(str(tmp_path / ".mcbbs_threads.json"))["news_a"]
        assert record["tid"] == 6816
        assert record["attempts"] == 0
        assert record["posted_at"] > _days_ago(1)

    def test_invalid_colour_is_not_registered(self, tmp_path):
        poster_obj = _make_poster({
            "max_age_days": 30,
            "colors": {"normal": "#123456"},
        })

        poster_obj._register_highlight(
            "news_a", str(tmp_path),
            poster.PostResult(url=f"{BASE}/thread-6816-1-1.html", tid=6816),
            "normal", "新帖")

        assert not (tmp_path / ".mcbbs_threads.json").exists()


class TestArtifactReleaseTime:
    def test_reads_release_date_field(self, tmp_path):
        (tmp_path / "news_a.json").write_text(
            json.dumps({"release_date": "2026-11-17T14:00:00Z"}), encoding="utf-8")
        stamp = poster._artifact_release_time(str(tmp_path), "news_a")
        assert time.strftime("%Y-%m-%d", time.localtime(stamp)) == "2026-11-17"

    def test_falls_back_to_date_in_stem(self, tmp_path):
        (tmp_path / "news_a.json").write_text("{}", encoding="utf-8")
        stamp = poster._artifact_release_time(
            str(tmp_path), "news_Something_2026-10-06_16_00_00")
        assert time.strftime("%Y-%m-%d", time.localtime(stamp)) == "2026-10-06"

    def test_falls_back_to_english_month_in_stem(self, tmp_path):
        (tmp_path / "news_a.json").write_text("{}", encoding="utf-8")
        stamp = poster._artifact_release_time(
            str(tmp_path), "news_Minecraft_Bedrock_Edition_26.63_Hotfix_Changelog_11_November_2026")
        assert time.strftime("%Y-%m-%d", time.localtime(stamp)) == "2026-11-11"

    def test_no_date_returns_none(self, tmp_path):
        (tmp_path / "news_a.json").write_text("{}", encoding="utf-8")
        assert poster._artifact_release_time(str(tmp_path), "news_Minecraft_Capes") is None
