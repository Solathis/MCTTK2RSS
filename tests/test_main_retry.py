"""test_main_retry.py — 处理失败的文章不应被永久标记为已处理

修复前的行为：只要 _process_single_article 被调用过，无论成功还是失败，
URL 都会进入 .state.json 的 posted_urls，导致该文章再也不会被重试。
"""
import json

import main

URL = "https://example.com/a"


def _seed_state(tmp_path, data=None):
    state_file = tmp_path / ".state.json"
    state_file.write_text(
        json.dumps(data if data is not None else {"posted_urls": [], "last_run": None}),
        encoding="utf-8",
    )
    return state_file


def _config(tmp_path, max_attempts=3):
    return {
        "output": {"save_dir": str(tmp_path)},
        "retry": {"max_article_attempts": max_attempts},
        "version_manifest": {"enabled": False},
    }


def _news():
    return [{"url": URL, "title": "Minecraft Java Edition 1.21.8", "_source": "minecraft_api"}]


def _read_state(state_file):
    return json.loads(state_file.read_text(encoding="utf-8"))


class TestFailedArticleNotMarkedPosted:
    def test_failure_is_not_marked_as_posted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_fetch_all_news", lambda config: _news())
        monkeypatch.setattr(main, "_process_single_article", lambda *a, **k: None)

        state_file = _seed_state(tmp_path)
        processed = main.run_scrape(_config(tmp_path), str(state_file))

        state = _read_state(state_file)
        assert state["posted_urls"] == [], "失败的文章不应被标记为已处理"
        assert state["failed_attempts"][URL] == 1
        assert processed == []

    def test_exception_also_counts_as_failure(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(main, "_fetch_all_news", lambda config: _news())
        monkeypatch.setattr(main, "_process_single_article", boom)

        state_file = _seed_state(tmp_path)
        main.run_scrape(_config(tmp_path), str(state_file))

        state = _read_state(state_file)
        assert state["posted_urls"] == []
        assert state["failed_attempts"][URL] == 1

    def test_failed_article_is_retried_on_next_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_fetch_all_news", lambda config: _news())
        monkeypatch.setattr(main, "_process_single_article", lambda *a, **k: None)

        state_file = _seed_state(tmp_path)
        config = _config(tmp_path)
        main.run_scrape(config, str(state_file))

        # 第二次运行时该 URL 仍应被视为"新新闻"
        result = main._filter_and_check_state(_news(), config, str(state_file))
        assert result is not None
        new_news, _, _ = result
        assert [n["url"] for n in new_news] == [URL]


class TestSuccessAndGiveUp:
    def test_success_is_marked_and_clears_failure_counter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_fetch_all_news", lambda config: _news())
        monkeypatch.setattr(
            main, "_process_single_article", lambda *a, **k: ("stem", "t.txt", "j.json")
        )

        state_file = _seed_state(
            tmp_path, {"posted_urls": [], "failed_attempts": {URL: 2}}
        )
        processed = main.run_scrape(_config(tmp_path), str(state_file))

        state = _read_state(state_file)
        assert state["posted_urls"] == [URL]
        assert state["failed_attempts"] == {}, "成功后应清理失败计数"
        assert processed == [("stem", "t.txt", "j.json")]

    def test_gives_up_after_max_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_fetch_all_news", lambda config: _news())
        monkeypatch.setattr(main, "_process_single_article", lambda *a, **k: None)

        state_file = _seed_state(tmp_path)
        config = _config(tmp_path, max_attempts=3)
        for _ in range(3):
            main.run_scrape(config, str(state_file))

        state = _read_state(state_file)
        assert state["posted_urls"] == [URL], "超过上限后应放弃，避免无限重试"
        assert state["failed_attempts"] == {}

    def test_max_attempts_of_one_gives_up_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_fetch_all_news", lambda config: _news())
        monkeypatch.setattr(main, "_process_single_article", lambda *a, **k: None)

        state_file = _seed_state(tmp_path)
        main.run_scrape(_config(tmp_path, max_attempts=1), str(state_file))

        state = _read_state(state_file)
        assert state["posted_urls"] == [URL]
        assert state["failed_attempts"] == {}
