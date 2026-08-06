#!/usr/bin/env python3
"""
main.py — MCTTK2RSS 新闻自动爬取 + 翻译 + RSS 生成 编排器

工作流程：
  1. 从 Minecraft 官方 API 获取最新新闻
  2. 按 news_types 配置过滤类型（Java 正式版/快照/预发布/RC、基岩版正式版/测试版）
  3. 检查已处理状态，跳过已处理的新闻
  4. 逐篇处理：解析 → 翻译 → 保存 JSON → 生成 RSS Feed

用法：
  python main.py                    # 全流程自动运行
  python main.py --dry-run          # 仅检测新新闻
  python main.py --rss-only         # 仅从 output 目录重新生成 RSS，不爬取

配置：
  统一使用 config.json（同目录下），不读取环境变量
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback

from log_setup import log_info, setup_logging
from rss_generator import generate_rss
from scraper import (
    FeedbackScraper,
    classify_news_type,
    download_header_image,
    get_latest_news_list,
    load_config,
    process_article,
    process_feedback_news,
    save_article_json,
)

# 项目根目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_main_config() -> dict:
    """加载统一配置"""
    config_path = os.path.join(PROJECT_DIR, "config.json")
    return load_config(config_path)


def filter_news_by_types(news_list: list, config: dict) -> list:
    """按配置的 news_types 过滤新闻"""
    news_types = config.get("news_types", {})
    # 如果没有配置 news_types 或全部为 true，不过滤
    if not news_types or all(news_types.values()):
        return news_list

    filtered = []
    for news in news_list:
        ntype = classify_news_type(news['title'])
        # 未识别类型默认跳过；Java-only 配置不能放行未知来源文章
        if ntype == "other":
            if news_types.get("other", False):
                filtered.append(news)
            continue
        if news_types.get(ntype, True):
            filtered.append(news)

    print(f"[过滤] {len(filtered)}/{len(news_list)} 条通过类型过滤")
    return filtered


def load_state(state_file: str) -> dict:
    """加载处理状态"""
    if os.path.exists(state_file):
        try:
            with open(state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            logging.warning("状态文件 %s 读取失败，将重置为初始状态", state_file, exc_info=True)
    return {"posted_urls": [], "last_run": None, "_first_run": True}


def save_state(state_file: str, state: dict):
    """保存处理状态"""
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 确保目录存在
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _fetch_all_news(config: dict) -> list:
    """获取所有来源的新闻（API + Feedback），合并返回"""
    all_news = []

    page_size = config.get("minecraft_api", {}).get("pageSize", 10)
    api_news = filter_news_by_types(get_latest_news_list(page_size=page_size, config=config), config)
    for news in api_news:
        news['_source'] = 'minecraft_api'
    all_news.extend(api_news)
    print(f"[主] API 新闻: {len(api_news)} 条")

    feedback_config = config.get('feedback_site', {})
    if feedback_config.get('enabled', False):
        try:
            scraper = FeedbackScraper(config)
            feedback_sections = scraper.get_latest_articles()
            for section_name, section_data in feedback_sections.items():
                for article in section_data['articles']:
                    article['_source'] = 'feedback'
                    article['section'] = section_name
                    article['section_cn'] = section_data['name_cn']
                    if article['url'].startswith('/'):
                        base = feedback_config.get('base_url', 'https://feedback.minecraft.net')
                        article['url'] = base + article['url']
                    if not article.get('release_date'):
                        article['release_date'] = ''
                    all_news.append(article)
            print(f"[主] Feedback 新闻: {sum(len(v['articles']) for v in feedback_sections.values())} 条")
        except ImportError as e:
            print(f"[主] Feedback 不可用: {e}")
        except Exception as e:
            print(f"[主] Feedback 获取失败: {e}")

    return all_news


def _filter_feedback_by_sections(feedback_items: list, config: dict) -> list:
    """只保留 config.json 明确启用的 Feedback section。"""
    sections = config.get("feedback_site", {}).get("sections", [])
    enabled_names = {section.get("name") for section in sections if section.get("enabled", True)}
    return [item for item in feedback_items if item.get("section") in enabled_names]


def _filter_and_check_state(all_news: list, config: dict, state_file: str):
    """
    类型过滤 + 加载状态 + 首次运行检测 + 过滤已处理。

    Returns:
        (new_news, state, posted_urls) 或 None（首次运行跳过）
    """
    api_items = [n for n in all_news if n.get('_source') == 'minecraft_api']
    feedback_items = _filter_feedback_by_sections(
        [n for n in all_news if n.get('_source') == 'feedback'], config
    )
    filtered_api = filter_news_by_types(api_items, config)
    filtered = filtered_api + feedback_items

    state = load_state(state_file)
    posted_urls = set(state.get("posted_urls", []))

    first_run_protection = config.get("first_run_protection", True)
    if first_run_protection and state.get("_first_run", False):
        print(f"[主] 检测到首次运行，将当前 {len(filtered)} 条新闻标记为已处理")
        posted_urls.update(n['url'] for n in filtered)
        state["posted_urls"] = list(posted_urls)
        state.pop("_first_run", None)
        save_state(state_file, state)

        save_dir = config["output"]["save_dir"]
        posted_state_file = os.path.join(save_dir, ".posted.json")
        if not os.path.exists(posted_state_file):
            with open(posted_state_file, "w", encoding="utf-8") as f:
                json.dump([], f)
            print(f"[主] 已创建 {posted_state_file}（空列表）")

        return None

    if state.get("_first_run", False):
        state.pop("_first_run", None)
        save_state(state_file, state)

    new_news = [n for n in filtered if n['url'] not in posted_urls]
    return new_news, state, posted_urls


def _process_single_article(news: dict, config: dict, save_dir: str) -> tuple | None:
    """
    处理单篇文章：解析 → 翻译 → 保存 JSON → 下载头图。

    Returns:
        (stem, json_path) 或 None
    """
    source = news.get('_source', 'minecraft_api')
    full_data = process_feedback_news(news, config) if source == 'feedback' else process_article(news, config=config)
    if not full_data:
        print("[主] 文章处理失败，跳过")
        return None

    json_path = save_article_json(full_data, save_dir=save_dir, config=config)
    if not json_path:
        print("[主] JSON 保存失败，跳过")
        return None

    # 下载头图（与 JSON 同名）
    header_image_url = full_data.get("header_image_url", "")
    if header_image_url:
        image_ext = ".jpg"
        try:
            url_path = header_image_url.split("?")[0]
            if "." in url_path:
                ext = url_path.rsplit(".", 1)[-1].lower()
                if ext in ["jpg", "jpeg", "png", "gif", "webp"]:
                    image_ext = f".{ext}"
        except Exception:  # noqa: BLE001
            logging.debug("图片扩展名解析失败，使用默认 .jpg", exc_info=True)
        base_path = json_path.rsplit(".", 1)[0]
        download_header_image(header_image_url, base_path + image_ext, config=config)

    stem = os.path.basename(json_path).rsplit(".", 1)[0]
    return stem, json_path


def run_scrape(config: dict, state_file: str, dry_run: bool = False) -> list:
    """
    执行爬取流程：获取新闻 → 过滤类型 → 检查状态 → 翻译 → 保存

    Returns:
        已处理的文章 (stem, txt_path, json_path) 列表
    """
    save_dir = config["output"]["save_dir"]
    os.makedirs(save_dir, exist_ok=True)

    all_news = _fetch_all_news(config)
    if not all_news:
        print("[主] 未获取到任何新闻")
        return []

    result = _filter_and_check_state(all_news, config, state_file)
    if result is None:
        return []
    new_news, state, posted_urls = result

    if not new_news:
        print(f"[主] 没有新新闻（共 {len(all_news)} 条，已全部处理过）")
        return []

    print(f"[主] 发现 {len(new_news)} 条新新闻待处理")

    if dry_run:
        print("\n[Dry Run] 新新闻列表：")
        for i, news in enumerate(new_news, 1):
            source = news.get('_source', 'minecraft_api')
            ntype = classify_news_type(news['title']) if source == 'minecraft_api' else 'feedback'
            print(f"  {i}. [{source}][{ntype}] {news['title']}")
            print(f"     {news['url']}")
        return []

    processed = []
    for i, news in enumerate(new_news, 1):
        source = news.get('_source', 'minecraft_api')
        print(f"\n{'=' * 60}")
        print(f"[主] 处理第 {i}/{len(new_news)} 条 [{source}]")
        print(f"{'=' * 60}")

        try:
            item = _process_single_article(news, config, save_dir)
            posted_urls.add(news['url'])
            state["posted_urls"] = list(posted_urls)
            save_state(state_file, state)
            if item:
                processed.append(item)
        except Exception as e:
            print(f"[主] 处理异常: {e}")
            traceback.print_exc()

    return processed


def run_rss(config: dict):
    """从 output 目录重新生成 RSS Feed"""
    save_dir = config["output"]["save_dir"]
    rss_config = config.get("rss", {})
    rss_output = rss_config.get("output_path", os.path.join(save_dir, "feed.xml"))
    generate_rss(
        save_dir=save_dir,
        output_path=rss_output,
        feed_title=rss_config.get("feed_title", "Minecraft News (中文翻译)"),
        feed_link=rss_config.get("feed_link", ""),
        feed_description=rss_config.get("feed_description", "Minecraft 官方新闻与更新日志的中文翻译 RSS"),
        max_items=rss_config.get("max_items", 50),
        max_age_days=rss_config.get("max_age_days", 90),
        site_base=config.get("minecraft_api", {}).get("site_base", "https://www.minecraft.net"),
    )


# ── 入口 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MCTTK2RSS — Minecraft 新闻自动爬取+翻译+RSS生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py                    # 全流程自动运行\n"
            "  python main.py --dry-run          # 仅检测新新闻\n"
            "  python main.py --rss-only         # 仅从 output 目录重新生成 RSS\n"
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="仅检测新新闻，不实际处理")
    parser.add_argument("--rss-only", action="store_true", help="仅从 output 目录重新生成 RSS，不爬取")
    parser.add_argument("--config", help="指定配置文件路径")
    args = parser.parse_args()

    # 设置输出编码
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

    # 初始化日志系统
    setup_logging(log_level=logging.DEBUG)

    print("=" * 60)
    print("  MCTTK2RSS — Minecraft 新闻自动爬取 + 翻译 + RSS 生成")
    print("=" * 60)

    log_info("程序启动")

    # 加载配置
    if args.config:
        from scraper import load_config
        config = load_config(args.config)
    else:
        config = load_main_config()

    save_dir = config["output"]["save_dir"]
    state_file = os.path.join(save_dir, ".state.json")

    # 检查必要的 API 配置（非 rss-only 模式需要）
    if not args.rss_only:
        api_key = config.get("openai_compat", {}).get("api_key", "")
        host = config.get("openai_compat", {}).get("host", "")
        if not api_key or "example" in host:
            print("\n[!] 请先在 config.json 中配置 openai_compat 部分")
            print("    至少需要: host, api_key, model")
            sys.exit(1)

    if args.rss_only:
        # 仅生成 RSS
        print("\n[模式] RSS Only — 从 output 目录重新生成 RSS\n")
        run_rss(config)
    elif args.dry_run:
        # 预览模式
        print("\n[模式] Dry Run — 仅检测新新闻\n")
        run_scrape(config, state_file, dry_run=True)
    else:
        # 全流程：爬取 → 翻译 → 保存 JSON → 生成 RSS
        print(f"\n[配置] 新闻目录: {save_dir}")
        news_types = config.get("news_types", {})
        enabled_types = [k for k, v in news_types.items() if v]
        print(f"[配置] 启用类型: {', '.join(enabled_types) if enabled_types else '全部'}")
        print()

        # 爬取
        processed = run_scrape(config, state_file)

        # 生成 RSS（无论有无新内容都重新生成，确保 feed.xml 始终最新）
        print(f"\n{'=' * 60}")
        print("  生成 RSS Feed")
        print(f"{'=' * 60}")
        run_rss(config)

        if not processed:
            print("\n[主] 没有新内容需要处理")

    print(f"\n{'=' * 60}")
    print("  完成！")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
