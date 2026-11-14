#!/usr/bin/env python3
"""Docker 定时调度器 - 每 10 分钟运行一次 main.py"""

import gc
import os
import subprocess
import sys
import time

INTERVAL = 600  # 10 分钟
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def run_main():
    """运行 main.py 并清理内存"""
    try:
        print(f"\n{'='*60}")
        print(f"[调度器] 开始执行 - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        result = subprocess.run(  # noqa: S603 - 命令由本文件常量拼出，不含外部输入
            [sys.executable, os.path.join(SCRIPT_DIR, "main.py")],
            cwd=SCRIPT_DIR,
            capture_output=False,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            print(f"[调度器] ⚠ main.py 退出码异常: {result.returncode}")
        else:
            print(f"\n[调度器] 执行完成 (退出码: {result.returncode})")

    except subprocess.TimeoutExpired:
        print("[调度器] ⚠ main.py 执行超时（600s），已强制终止")
    except Exception as e:
        print(f"[调度器] 执行失败: {e}")
    finally:
        # 强制垃圾回收
        gc.collect()

if __name__ == "__main__":
    print("[调度器] 启动 - 每 10 分钟运行一次")

    while True:
        run_main()
        print(f"[调度器] 等待 {INTERVAL} 秒...")
        time.sleep(INTERVAL)
