"""test_logging_fallback.py — log_setup 不可用时 scraper 的日志回退实现

只在独立拷贝 scraper.py、日志模块缺失时才走回退分支；用子进程把
`log_setup` 置空来模拟，避免污染测试进程自己的模块缓存。
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestScraperLoggingFallback:
    def test_fallback_accepts_format_args(self):
        """回退的 log_* 必须和 log_setup 一样接受 (message, *args)。

        回归：回退实现曾只收一个参数，`log_info("... %s", value)` 会 TypeError，
        把新闻发现（例如版本清单探测）直接打断。
        """
        code = (
            "import sys; sys.modules['log_setup'] = None; "
            "import scraper; "
            "scraper.log_info('[info] %s', 'ok'); "
            "scraper.log_error('[error] %s', 'bad', exc_info=True); "
            "scraper.log_debug('[debug] %s', 'dbg'); "
            "scraper.log_warning('[warn] %s', 'warn')"
        )
        result = subprocess.run(  # noqa: S603 - 命令由本文件常量拼出，不含外部输入
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "[INFO] [info] ok" in result.stdout
        assert "[ERROR] [error] bad" in result.stdout
        assert "[DEBUG] [debug] dbg" in result.stdout
        assert "[WARNING] [warn] warn" in result.stdout
