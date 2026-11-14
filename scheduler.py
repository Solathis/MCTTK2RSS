#!/usr/bin/env python3
"""Docker 定时调度器 — 周期性运行 main.py，并把调度事件写进同一份日志。

环境变量：
  MCTTK_INTERVAL     两轮之间的间隔秒数（默认 3600 = 1 小时）
  MCTTK_RUN_TIMEOUT  单轮 main.py 的超时秒数（默认 3600 = 1 小时）
"""

import gc
import os
import subprocess
import sys
import time

from log_setup import log_error, log_info, log_section, log_warning, setup_logging

INTERVAL = max(int(os.environ.get("MCTTK_INTERVAL") or 3600), 10)
RUN_TIMEOUT = max(int(os.environ.get("MCTTK_RUN_TIMEOUT") or 3600), 60)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run_main():
    """运行 main.py 并清理内存"""
    started = time.monotonic()
    log_section("调度: 开始执行 main.py")
    try:
        result = subprocess.run(  # noqa: S603 - 命令由本文件常量拼出，不含外部输入
            [sys.executable, os.path.join(SCRIPT_DIR, "main.py")],
            cwd=SCRIPT_DIR,
            capture_output=False,
            text=True,
            timeout=RUN_TIMEOUT,
        )
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            log_warning("[调度] main.py 退出码异常: %s（用时 %.1fs）", result.returncode, elapsed)
        else:
            log_info("[调度] main.py 执行完成（退出码 0，用时 %.1fs）", elapsed)

    except subprocess.TimeoutExpired:
        log_error("[调度] main.py 执行超时（%d 秒），已强制终止", RUN_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - 调度器不能因为单轮异常退出
        log_error("[调度] 执行失败: %s", e, exc_info=True)
    finally:
        # 强制垃圾回收
        gc.collect()


if __name__ == "__main__":
    setup_logging()
    log_info("[调度] 启动：每 %d 秒运行一次 main.py，单轮超时 %d 秒", INTERVAL, RUN_TIMEOUT)

    while True:
        run_main()
        log_info("[调度] 等待 %d 秒后开始下一轮", INTERVAL)
        time.sleep(INTERVAL)
