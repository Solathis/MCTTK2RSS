"""test_logging.py — 日志基础设施与「运行汇总/发帖地址」日志的回归测试

这些测试锁定用户最关心的几件事：
  - 日志确实写进 logs/mcttk_YYYY-MM-DD.log
  - print() 与 stderr 的内容也会落盘（否则容器里只剩 docker logs 里有信息）
  - 旧日志按保留天数清理
  - 运行摘要、每篇处理结果、发帖成功与帖子地址都能在日志里查到
"""

import json
import sys
from datetime import datetime, timedelta

import pytest

import log_setup
import main
import poster


@pytest.fixture
def log_dir(tmp_path):
    """每个用例独立日志目录，结束后恢复全局日志状态。"""
    directory = tmp_path / "logs"
    yield directory
    log_setup.reset_logging()


def _today_log(log_dir) -> str:
    path = log_dir / f"mcttk_{datetime.now():%Y-%m-%d}.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── 基础设施 ──────────────────────────────────────────

class TestLogFile:
    def test_writes_daily_file(self, log_dir):
        log_setup.setup_logging(
            log_dir=str(log_dir), console_output=False, capture_streams=False
        )
        log_setup.log_info("hello %s", "世界")

        assert _today_log(log_dir).count("hello 世界") == 1
        assert log_setup.current_log_path() == str(log_dir / f"mcttk_{datetime.now():%Y-%m-%d}.log")

    def test_print_and_stderr_are_captured(self, log_dir):
        log_setup.setup_logging(
            log_dir=str(log_dir), console_output=False, capture_streams=True
        )
        print("来自 print 的一行")
        print("写到 stderr 的告警", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()

        content = _today_log(log_dir)
        assert "[STDOUT] 来自 print 的一行" in content
        assert "[STDERR] 写到 stderr 的告警" in content

    def test_level_from_parameter(self, log_dir):
        log_setup.setup_logging(
            log_dir=str(log_dir), log_level=log_setup.logging.WARNING,
            console_output=False, capture_streams=False,
        )
        log_setup.log_info("这条不该出现")
        log_setup.log_warning("这条应该出现")

        content = _today_log(log_dir)
        assert "这条不该出现" not in content
        assert "这条应该出现" in content

    def test_unconfigured_logger_still_prints_to_stdout(self, capsys):
        log_setup.reset_logging()
        log_setup.log_info("兜底输出")
        assert "兜底输出" in capsys.readouterr().out


class TestRetention:
    def test_old_files_are_pruned(self, log_dir):
        log_dir.mkdir(parents=True, exist_ok=True)
        old = log_dir / f"mcttk_{(datetime.now() - timedelta(days=30)):%Y-%m-%d}.log"
        old.write_text("old", encoding="utf-8")

        log_setup.setup_logging(
            log_dir=str(log_dir), days_to_keep=7,
            console_output=False, capture_streams=False,
        )

        assert not old.exists()
        assert (log_dir / f"mcttk_{datetime.now():%Y-%m-%d}.log").exists()

    def test_file_switches_at_midnight(self, log_dir, monkeypatch):
        """长跑进程跨过午夜后，新日志必须写到新日期的文件里。"""
        fake_now = {"value": datetime(2026, 11, 14, 23, 59, 58)}
        real_datetime = log_setup.datetime

        class FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now["value"]

        monkeypatch.setattr(log_setup, "datetime", FakeDatetime)

        log_setup.setup_logging(
            log_dir=str(log_dir), console_output=False, capture_streams=False
        )
        log_setup.log_info("午夜前")
        fake_now["value"] = datetime(2026, 11, 15, 0, 0, 2)
        log_setup.log_info("午夜后")

        assert (log_dir / "mcttk_2026-11-14.log").read_text(encoding="utf-8").strip().endswith("午夜前")
        assert "午夜后" in (log_dir / "mcttk_2026-11-15.log").read_text(encoding="utf-8")


# ── 运行摘要 ──────────────────────────────────────────

def _scrape_config(tmp_path):
    return {
        "output": {"save_dir": str(tmp_path / "output")},
        "retry": {"max_article_attempts": 2},
        "version_manifest": {"enabled": False},
    }


def _seed_state(tmp_path):
    out = tmp_path / "output"
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / ".state.json"
    state_file.write_text(
        json.dumps({"posted_urls": [], "last_run": None, "failed_attempts": {}}),
        encoding="utf-8",
    )
    return state_file


class TestRunSummary:
    def test_scrape_logs_counts_and_success(self, tmp_path, log_dir, monkeypatch):
        log_setup.setup_logging(
            log_dir=str(log_dir), console_output=False, capture_streams=False
        )
        url = "https://example.com/news-1"
        monkeypatch.setattr(
            main, "_fetch_all_news",
            lambda config: [{"url": url, "title": "示例新闻", "_source": "minecraft_api"}],
        )
        monkeypatch.setattr(
            main, "_process_single_article", lambda *a, **k: ("stem", "s.txt", "s.json")
        )

        stats = {}
        processed = main.run_scrape(_scrape_config(tmp_path), str(_seed_state(tmp_path)), stats=stats)

        assert processed == [("stem", "s.txt", "s.json")]
        assert stats == {"found": 1, "new": 1, "processed": 1}

        content = _today_log(log_dir)
        assert "状态检查: 候选 1 条，已处理 0 条，本次待处理 1 条" in content
        assert "处理成功: stem" in content
        assert "爬取阶段完成: 待处理 1 条，成功 1 条，失败 0 条" in content

    def test_scrape_logs_failure_retry_hint(self, tmp_path, log_dir, monkeypatch):
        log_setup.setup_logging(
            log_dir=str(log_dir), console_output=False, capture_streams=False
        )
        monkeypatch.setattr(
            main, "_fetch_all_news",
            lambda config: [{"url": "https://example.com/x", "title": "x", "_source": "minecraft_api"}],
        )
        monkeypatch.setattr(main, "_process_single_article", lambda *a, **k: None)

        main.run_scrape(_scrape_config(tmp_path), str(_seed_state(tmp_path)))

        assert "处理失败（第 1/2 次），下次运行将重试" in _today_log(log_dir)

    def test_post_logs_url(self, tmp_path, log_dir, monkeypatch):
        log_setup.setup_logging(
            log_dir=str(log_dir), console_output=False, capture_streams=False
        )

        class FakePoster:
            def __init__(self, cfg):
                pass

            def login(self):
                return True

            def post_news_file(self, stem, txt_path, json_path, news_dir, no_image=False, attach_json=True):
                return "https://www.mcbbs.co/thread-777-1-1.html"

        monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
        monkeypatch.setattr(
            poster, "load_poster_config",
            lambda path=None: {"base_url": "https://www.mcbbs.co", "forum_fid": 45},
        )
        monkeypatch.setattr(poster, "load_posted", lambda path: set())
        monkeypatch.setattr(poster, "save_posted", lambda path, posted: None)
        monkeypatch.setattr(main.time, "sleep", lambda *_: None)

        config = {
            "output": {"save_dir": str(tmp_path)},
            "mcbbs": {"enabled": True},
        }
        result = main.run_post([("stem", "s.txt", "s.json")], config)

        assert result == {
            "success": 1, "failed": 0,
            "urls": ["https://www.mcbbs.co/thread-777-1-1.html"],
        }
        content = _today_log(log_dir)
        assert "发帖成功: https://www.mcbbs.co/thread-777-1-1.html" in content
        assert "发布完成: 成功 1，失败 0" in content
