#!/usr/bin/env python3
"""
init_state.py — 初始化 output/.state.json 和 output/.posted.json

用途：测试前把当前所有来源的新闻标记为"已处理"，只保留选中的一条，避免全量翻译。

采集直接复用 main._fetch_all_news()，与主流程保持同一口径（官方 API / Java 版本
清单 / Feedback 三个来源，按 URL 去重）——否则工具看不到 manifest 候选，标记
出来的状态会和线上实际会处理的内容不一致。

用法：
  python init_state.py
"""

import json
import os
import sys
import time
from collections import Counter

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from log_setup import setup_logging  # noqa: E402
from main import _fetch_all_news, _news_type_of  # noqa: E402
from scraper import load_config  # noqa: E402
from utils import CONFIG_DIR, load_dotenv  # noqa: E402

load_dotenv(PROJECT_DIR)

CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
config = load_config(CONFIG_PATH)
save_dir = config["output"]["save_dir"]
os.makedirs(save_dir, exist_ok=True)

# 采集过程的日志只写文件，不打断下面的交互式列表
setup_logging(console_output=False, capture_streams=False)

print("正在采集新闻（官方 API / Java 版本清单 / Feedback）...")
all_news = _fetch_all_news(config)

if not all_news:
    print("未获取到任何新闻，退出")
    sys.exit(1)

source_counts = Counter(news.get("_source", "?") for news in all_news)
detail = "，".join(f"{source} {count} 条" for source, count in source_counts.items())
print(f"共 {len(all_news)} 条（{detail}）\n")

# ── 列出所有新闻 ──
for i, news in enumerate(all_news):
    print(f"  {i+1:2d}. [{news.get('_source', '?')}][{_news_type_of(news)}] {news['title']}")
    print(f"       {news['url']}")

# ── 选择保留的新闻 ──
print()
choice = input("请输入要保留（不标记为已处理）的新闻编号（1 开始），直接回车默认保留第 1 条: ").strip()
if not choice:
    keep_idx = 0
else:
    try:
        keep_idx = int(choice) - 1
        if not (0 <= keep_idx < len(all_news)):
            print("编号超出范围，默认保留第 1 条")
            keep_idx = 0
    except ValueError:
        print("输入无效，默认保留第 1 条")
        keep_idx = 0

kept = all_news[keep_idx]
to_mark = [n for i, n in enumerate(all_news) if i != keep_idx]

print(f"\n保留：{kept['title']}")
print(f"标记为已处理：{len(to_mark)} 条")

# ── 写入 .state.json ──
state_file = os.path.join(save_dir, ".state.json")
state = {
    "posted_urls": [n["url"] for n in to_mark],
    "last_run": time.strftime("%Y-%m-%dT%H:%M:%S")
}
with open(state_file, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
print(f"\n已写入 {state_file}（{len(to_mark)} 条 URL）")

# ── 写入 .posted.json ──
# .posted.json 存的是已发布的文件 stem，此处无实际文件，留空即可
# 只需确保文件存在（空列表），poster 会正常工作
posted_file = os.path.join(save_dir, ".posted.json")
if not os.path.exists(posted_file):
    with open(posted_file, "w", encoding="utf-8") as f:
        json.dump([], f)
    print(f"已创建 {posted_file}（空列表）")
else:
    print(f"{posted_file} 已存在，未修改")

print("\n完成。现在运行 python main.py 将只处理保留的那条新闻。")
