"""test_main_bili.py — main.py 的 B站专栏发布编排

全部用假发帖器：不联网、不真投稿；断言编排行为（挑哪些篇、记什么账、什么时候停）。
"""
import json

import pytest

import main
from bili import poster as bili_poster

DRAFT = {
    "ok": True, "state_desc": "已保存草稿", "pending_review": False,
    "url": None, "edit_url": "https://member.bilibili.com/article-text/home?aid=1",
}
LIVE = {
    "ok": True, "state_desc": "已发布", "pending_review": False,
    "url": "https://www.bilibili.com/read/cv123", "edit_url": "",
}
REVIEW = {
    "ok": True, "state_desc": "待审核", "pending_review": True,
    "url": None, "edit_url": "https://member.bilibili.com/article-text/home?aid=2",
}
FAILED = {
    "ok": False, "state_desc": "", "pending_review": False,
    "url": None, "edit_url": "", "error": "code=-400 参数错误",
}


class FakeBiliPoster:
    """记录调用；outcomes 按顺序返回（不够时重复最后一个）。"""

    instances = []
    default_outcomes = [LIVE]
    login_error = None

    def __init__(self, bili_config, http_config=None, session=None):
        self.cfg = bili_config
        self.http_cfg = http_config
        self.outcomes = list(FakeBiliPoster.default_outcomes)
        self.login_error = FakeBiliPoster.login_error
        self.posted_stems = []
        self.html_paths = []
        self.cover_paths = []
        FakeBiliPoster.instances.append(self)

    def check_login(self):
        if self.login_error:
            raise RuntimeError(self.login_error)
        return {"isLogin": True, "mid": 1, "uname": "tester"}

    def post_news_file(self, stem, json_path, html_path, cover_path=None):
        self.posted_stems.append(stem)
        self.html_paths.append(html_path)
        self.cover_paths.append(cover_path)
        index = min(len(self.posted_stems) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


@pytest.fixture(autouse=True)
def fake_poster(monkeypatch):
    FakeBiliPoster.instances = []
    FakeBiliPoster.default_outcomes = [LIVE]
    FakeBiliPoster.login_error = None
    monkeypatch.setattr(bili_poster, "BilibiliPoster", FakeBiliPoster)
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)


@pytest.fixture
def save_dir(tmp_path, monkeypatch):
    directory = tmp_path / "output"
    directory.mkdir()
    monkeypatch.setattr(main, "_load_modules_config", lambda: None)
    return directory


def _make_config(save_dir, *, enabled=True, **bili_overrides):
    bili = {
        "enabled": enabled,
        "cookie_file": "",
        "submit": "draft",
        "category_id": 26,
        "max_per_run": 0,
        "interval_seconds": 0,
        "title_suffix": " [AI翻译]",
        "max_title_chars": 80,
    }
    bili.update(bili_overrides)
    return {
        "output": {"save_dir": str(save_dir)},
        "http": {},
        "mcbbs": {"enabled": False},
        "bilibili": bili,
    }


def _write_article(save_dir, stem, *, with_html=True, with_opus=False):
    json_path = save_dir / f"{stem}.json"
    json_path.write_text(json.dumps({
        "title": "Title",
        "translated_title": "标题",
        "blocks": [{"type": "p", "source_text": "Hello", "translated_text": "你好", "meta": {}}],
    }, ensure_ascii=False), encoding="utf-8")
    (save_dir / f"{stem}.txt").write_text("bbcode", encoding="utf-8")
    if with_html:
        (save_dir / f"{stem}.html").write_text("<p>你好</p>", encoding="utf-8")
    if with_opus:
        (save_dir / f"{stem}.opus.json").write_text(
            json.dumps({"paragraphs": [{"para_type": 1, "format": {"indent": {}}, "text": {"nodes": []}}]},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    return (stem, str(save_dir / f"{stem}.txt"), str(json_path))


def _state(save_dir):
    path = save_dir / ".posted_bili.json"
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))


# ── run_post_bili ────────────────────────────────────

class TestRunPostBili:
    def test_disabled_skips_before_login(self, save_dir):
        item = _write_article(save_dir, "news_a")

        result = main.run_post_bili([item], _make_config(save_dir, enabled=False))

        assert result == {"success": 0, "failed": 0, "pending_review": 0, "urls": [], "drafts": []}
        assert FakeBiliPoster.instances == []

    def test_draft_recorded_with_edit_url(self, save_dir):
        FakeBiliPoster.default_outcomes = [DRAFT]
        item = _write_article(save_dir, "news_a")

        result = main.run_post_bili([item], _make_config(save_dir))

        assert result["success"] == 1
        assert result["drafts"] == [DRAFT["edit_url"]]
        assert result["urls"] == []
        assert _state(save_dir) == {"news_a"}

    def test_live_url_and_pending_review_counted(self, save_dir):
        FakeBiliPoster.default_outcomes = [LIVE, REVIEW]
        items = [_write_article(save_dir, "news_a"), _write_article(save_dir, "news_b")]

        result = main.run_post_bili(items, _make_config(save_dir))

        assert result["success"] == 2
        assert result["urls"] == [LIVE["url"]]
        assert result["pending_review"] == 1

    def test_already_posted_is_skipped(self, save_dir):
        item = _write_article(save_dir, "news_a")
        (save_dir / ".posted_bili.json").write_text('["news_a"]', encoding="utf-8")

        result = main.run_post_bili([item], _make_config(save_dir))

        assert result["success"] == 0
        assert FakeBiliPoster.instances == []

    def test_first_enable_seeds_existing_articles(self, save_dir):
        _write_article(save_dir, "news_old")
        item = _write_article(save_dir, "news_new")

        main.run_post_bili([item], _make_config(save_dir))

        # 历史文章被标记为已投稿，本次新文章正常投出去
        assert _state(save_dir) == {"news_old", "news_new"}
        assert FakeBiliPoster.instances[0].posted_stems == ["news_new"]

    def test_login_failure_marks_whole_batch_failed(self, save_dir):
        FakeBiliPoster.login_error = "cookie 已失效"
        items = [_write_article(save_dir, "news_a"), _write_article(save_dir, "news_b")]

        result = main.run_post_bili(items, _make_config(save_dir))

        assert result["failed"] == 2
        assert result["success"] == 0
        assert _state(save_dir) == set()
        assert FakeBiliPoster.instances[0].posted_stems == []

    def test_max_per_run_caps_queue(self, save_dir):
        items = [_write_article(save_dir, f"news_{i}") for i in range(3)]

        result = main.run_post_bili(items, _make_config(save_dir, max_per_run=1))

        assert result["success"] == 1
        assert FakeBiliPoster.instances[0].posted_stems == ["news_0"]
        assert _state(save_dir) == {"news_0"}

    def test_failed_outcome_not_recorded(self, save_dir):
        FakeBiliPoster.default_outcomes = [FAILED]
        item = _write_article(save_dir, "news_a")

        result = main.run_post_bili([item], _make_config(save_dir))

        assert result["failed"] == 1
        assert _state(save_dir) == set()

    def test_missing_html_is_generated_on_demand(self, save_dir, monkeypatch):
        item = _write_article(save_dir, "news_a", with_html=False)
        created = []

        def fake_convert(json_path, output_prefix=None, modules_config=None):
            path = output_prefix + ".html"
            with open(path, "w", encoding="utf-8") as f:
                f.write("<p>现场生成</p>")
            created.append(path)
            return path

        monkeypatch.setattr(main, "convert_bili_html_file", fake_convert)

        result = main.run_post_bili([item], _make_config(save_dir))

        assert result["success"] == 1
        assert created == [str(save_dir / "news_a.html")]
        assert FakeBiliPoster.instances[0].html_paths[0].endswith("news_a.html")

    def test_html_generation_failure_marks_failure(self, save_dir, monkeypatch):
        item = _write_article(save_dir, "news_bad", with_html=False)

        def boom(*_args, **_kwargs):
            raise RuntimeError("转换炸了")

        monkeypatch.setattr(main, "convert_bili_html_file", boom)

        result = main.run_post_bili([item], _make_config(save_dir))

        assert result["failed"] == 1
        assert FakeBiliPoster.instances[0].posted_stems == []

    def test_interval_sleeps_between_posts_only(self, save_dir, monkeypatch):
        items = [_write_article(save_dir, "news_a"), _write_article(save_dir, "news_b")]
        slept = []
        monkeypatch.setattr(main.time, "sleep", slept.append)

        main.run_post_bili(items, _make_config(save_dir, interval_seconds=7))

        assert slept == [7]  # 两篇之间睡一次，最后一篇之后不睡

    def test_opus_renderer_posts_opus_artifact(self, save_dir):
        item = _write_article(save_dir, "news_a", with_html=False, with_opus=True)

        result = main.run_post_bili([item], _make_config(save_dir, renderer="opus"))

        assert result["success"] == 1
        assert FakeBiliPoster.instances[0].html_paths[0].endswith("news_a.opus.json")

    def test_opus_artifact_generated_on_demand(self, save_dir, monkeypatch):
        item = _write_article(save_dir, "news_a", with_html=False, with_opus=False)
        created = []

        def fake_convert(json_path, output_prefix=None, modules_config=None):
            path = output_prefix + ".opus.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"paragraphs": []}, f)
            created.append(path)
            return path

        monkeypatch.setattr(main, "convert_bili_opus_file", fake_convert)

        result = main.run_post_bili([item], _make_config(save_dir, renderer="opus"))

        assert result["success"] == 1
        assert created == [str(save_dir / "news_a.opus.json")]
        assert FakeBiliPoster.instances[0].html_paths[0].endswith("news_a.opus.json")

    def test_renderer_defaults_to_html(self):
        assert main._bili_renderer({"bilibili": {"renderer": "weird"}}) == "html"
        assert main._bili_renderer({"bilibili": {}}) == "html"
        assert main._bili_renderer({"bilibili": {"renderer": "OPUS"}}) == "opus"
        assert main._bili_content_suffix("opus") == ".opus.json"
        assert main._bili_content_suffix("html") == ".html"

    def test_cover_image_passed_when_present(self, save_dir):
        item = _write_article(save_dir, "news_a")
        (save_dir / "news_a.jpg").write_bytes(b"\xff\xd8\xff")

        main.run_post_bili([item], _make_config(save_dir))

        assert FakeBiliPoster.instances[0].cover_paths[0].endswith("news_a.jpg")


# ── run_post_only_bili ───────────────────────────────

class TestRunPostOnlyBili:
    def test_posts_unposted_articles(self, save_dir):
        _write_article(save_dir, "news_a")
        _write_article(save_dir, "news_b")
        (save_dir / ".posted_bili.json").write_text('["news_b"]', encoding="utf-8")
        (save_dir / ".state.json").write_text("{}", encoding="utf-8")

        result = main.run_post_only_bili(_make_config(save_dir))

        assert result["success"] == 1
        assert FakeBiliPoster.instances[0].posted_stems == ["news_a"]
        assert _state(save_dir) == {"news_a", "news_b"}

    def test_disabled(self, save_dir):
        result = main.run_post_only_bili(_make_config(save_dir, enabled=False))
        assert result["success"] == 0
        assert FakeBiliPoster.instances == []

    def test_empty_output(self, save_dir):
        result = main.run_post_only_bili(_make_config(save_dir))
        assert result["success"] == 0
        assert FakeBiliPoster.instances == []


# ── 配置摘要 ─────────────────────────────────────────

class TestBiliConfigSummary:
    def test_disabled(self):
        assert main._bili_config_summary({"bilibili": {"enabled": False}}) == "禁用"

    def test_enabled_without_cookie(self, monkeypatch):
        monkeypatch.delenv("BILI_SESSDATA", raising=False)
        monkeypatch.delenv("BILI_JCT", raising=False)
        summary = main._bili_config_summary({"bilibili": {
            "enabled": True, "cookie_file": "config/does-not-exist.json",
            "category_id": 26, "submit": "draft", "max_per_run": 3,
        }})
        assert "tools/bili_login.py" in summary
        assert "分区 26" in summary
