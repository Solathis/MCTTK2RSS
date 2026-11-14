#!/usr/bin/env python3
"""Docker 定时调度器 — 按本地时间对齐运行 main.py，并把调度事件写进同一份日志。

环境变量：
  MCTTK_INTERVAL     两轮之间的间隔秒数（默认 3600 = 1 小时），同时作为对齐网格
  MCTTK_RUN_TIMEOUT  单轮 main.py 的超时秒数（默认 3600 = 1 小时）
  TZ                 容器时区（Docker 默认 UTC；北京时间用 Asia/Shanghai）

对齐规则：以本地时间当天 00:00 为网格起点，每轮结束后等到下一个 INTERVAL 的
整数倍时刻再跑（3600 → 整点触发，600 → 每 10 分钟）。这样单轮耗时会累积成
漂移的问题就没有了；启动后仍会立刻跑第一轮，便于重启/升级后马上补齐。
"""

import datetime as dt
import gc
import os
import subprocess
import sys
import time

from log_setup import log_error, log_info, log_section, log_warning, setup_logging

INTERVAL = max(int(os.environ.get("MCTTK_INTERVAL") or 3600), 10)
RUN_TIMEOUT = max(int(os.environ.get("MCTTK_RUN_TIMEOUT") or 3600), 60)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def seconds_until_next_slot(interval: int, now: dt.datetime = None) -> float:
    """返回到下一个对齐时刻还要等多少秒。

    网格起点是本地时间当天 00:00，所以 interval=3600 落在整点、600 落在
    10 分钟整。结果最小 1 秒：恰好站在边界上时等完整间隔，不会连跑两轮。
    """
    now = now or dt.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    return max(interval - (elapsed % interval), 1.0)


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
    log_info("[调度] 启动：每 %d 秒对齐运行一次 main.py，单轮超时 %d 秒，容器时区 %s"
             "（Docker 默认 UTC，可用 TZ 覆盖）",
             INTERVAL, RUN_TIMEOUT, dt.datetime.now().astimezone().strftime("%Z%z") or "未知")

    while True:
        run_main()

        wait = seconds_until_next_slot(INTERVAL)
        next_run = dt.datetime.now() + dt.timedelta(seconds=wait)
        log_info("[调度] 下一轮 %s（本地时间；UTC %s），等待 %d 秒",
                 next_run.strftime("%Y-%m-%d %H:%M:%S"),
                 next_run.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
                 round(wait))
        time.sleep(wait)
