#!/usr/bin/env python3
"""Docker 定时调度器 — 从 config.json 读取 cron 规则并按间隔运行 main.py"""

import gc
import json
import os
import subprocess
import sys
import time

# cron 解析：将 5 字段 cron 表达式解析为秒级间隔
# 支持格式: "0 */6 * * *" "0 0,6,12,18 * * *" "0 0 * * *" 等

_CRON_FIELD_COUNT = 5
# cron 每个字段代表的时间单位（秒）
_CRON_FIELD_SECONDS = [86400 // 24, 3600, 86400, 86400 // 32, 86400]


def _parse_cron_to_interval(cron_expr: str, fallback: int = 21600) -> int:
    """
    将 cron 表达式解析为大致的执行间隔（秒）。

    仅解析间隔型 cron（如 "0 */6 * * *" -> 21600 秒），
    复杂表达式回退到 fallback。

    Returns:
        秒级间隔
    """
    parts = cron_expr.strip().split()
    if len(parts) != _CRON_FIELD_COUNT:
        return fallback

    # 找到第一个有 */N 或具体列表的字段
    for i, val in enumerate(parts):
        if "/" in val:
            # */N 或 M-N/S
            try:
                step = int(val.rsplit("/", 1)[1].strip())
                if step > 0 and i < 3:  # minute/hour/day only
                    # 如果 hour 是 */6, interval = 6 * 3600
                    # 如果 minute 是 */N, interval = N * 60
                    if i == 1:  # hour field
                        return step * 3600
                    if i == 0:  # minute field
                        return step * 60
                    if i == 2:  # day field
                        return step * 86400
            except ValueError:
                pass
    return fallback


def load_scheduler_config() -> dict:
    """从 config.json 读取调度器配置"""
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"
    )
    defaults = {
        "cron": "0 */6 * * *",
        "interval_seconds": 21600,
        "timeout_seconds": 600,
    }
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                full_cfg = json.load(f)
            sched = full_cfg.get("scheduler", {})
            defaults.update(sched)
        except (OSError, ValueError) as e:
            print(f"[调度器] 配置加载失败，使用默认值: {e}")
    return defaults


def run_main(timeout_seconds: int):
    """运行 main.py 并清理内存"""
    try:
        print(f"\n{'=' * 60}")
        print(f"[调度器] 开始执行 - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'=' * 60}")

        result = subprocess.run(
            [sys.executable, "main.py"],
            capture_output=False,
            text=True,
            timeout=timeout_seconds,
        )

        if result.returncode != 0:
            print(f"[调度器] main.py 退出码异常: {result.returncode}")
        else:
            print(f"\n[调度器] 执行完成 (退出码: {result.returncode})")

    except subprocess.TimeoutExpired:
        print(f"[调度器] main.py 执行超时({timeout_seconds}s)，已强制终止")
    except Exception as e:  # noqa: BLE001
        print(f"[调度器] 执行失败: {e}")
    finally:
        gc.collect()


if __name__ == "__main__":
    cfg = load_scheduler_config()
    interval = cfg.get("interval_seconds") or _parse_cron_to_interval(
        cfg.get("cron", "0 */6 * * *")
    )
    run_timeout = cfg.get("timeout_seconds", 600)
    cron_expr = cfg.get("cron", "0 */6 * * *")

    print("[调度器] 启动")
    print(f"[调度器] Cron 规则: {cron_expr}")
    print(f"[调度器] 执行间隔: {interval} 秒 ({interval // 3600} 小时)")
    print(f"[调度器] 单次超时: {run_timeout} 秒")

    while True:
        run_main(run_timeout)
        print(f"[调度器] 等待 {interval} 秒...")
        time.sleep(interval)
