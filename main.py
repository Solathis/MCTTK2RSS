#!/usr/bin/env python3
"""
main.py — MCTTK 新闻自动爬取 + 翻译 + 转换 + 发布 编排器

工作流程：
  1. 从 Minecraft 官方 API 获取最新新闻
  2. 按 news_types 配置过滤类型（Java 正式版/快照/预发布/RC、基岩版正式版/测试版）
  3. 检查已发布状态，跳过已处理的新闻
  4. 逐篇处理：解析 → 翻译 → 保存 JSON → 转换 BBCode/Markdown（+ 专栏 HTML）→ 发布
  5. 记录运行汇总（来源条数 / 新增 / 翻译篇数 / 各平台发帖成功失败 / 发帖地址 / 耗时）

发布平台按配置是否启用来决定，用 --publish 可以进一步只投其中一个：
  mcbbs     → poster.py（BBCode + 附件 + 验证码登录）
  bilibili  → bili/poster.py（专栏 HTML + cookie 会话，正文图片重托管到 B站 CDN）

用法：
  python main.py                    # 自动运行全流程
  python main.py --dry-run          # 仅检测，不翻译也不发布
  python main.py --scrape-only      # 只爬取+翻译+转换，不发布
  python main.py --post-only        # 只发布 output 目录中未发布的文件
  python main.py --publish bilibili # 只投专栏（不碰 MCBBS）

配置：
  统一使用 config/config.json
  环境变量覆盖：OPENAI_API_KEY, MCBBS_USERNAME, MCBBS_PASSWORD, BILI_SESSDATA, BILI_JCT 等
  日志：logs/mcttk_YYYY-MM-DD.log，级别用 MCTTK_LOG_LEVEL 调整
"""

import argparse
import glob
import io
import json
import os
import sys
import time
from collections import Counter

from converter import convert_bili_html_file, convert_bili_opus_file, convert_json_file
from log_setup import (
    current_log_path,
    elapsed_since,
    human_size,
    log_debug,
    log_error,
    log_info,
    log_section,
    log_warning,
    setup_logging,
)
from scraper import (
    FeedbackScraper,
    classify_news_type,
    download_header_image,
    get_java_news_from_manifest,
    get_latest_news_list,
    load_config,
    process_article,
    process_feedback_news,
    save_article_json,
)
from utils import (
    CONFIG_DIR,
    classify_article_type,
    find_image,
    load_dotenv,
    load_posted,
    load_thread_records,
    save_json_atomic,
    save_posted,
    thread_records_path,
)

# 项目根目录（源码所在目录；配置统一放 config/，路径由 utils.CONFIG_DIR 给出）
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(PROJECT_DIR)


def load_main_config() -> dict:
    """加载统一配置"""
    config_path = os.path.join(CONFIG_DIR, "config.json")
    return load_config(config_path)


def filter_news_by_types(news_list: list, config: dict) -> list:
    """按配置的 news_types 过滤新闻"""
    news_types = config.get("news_types", {})
    # 如果没有配置 news_types 或全部为 true，不过滤
    if not news_types or all(news_types.values()):
        log_debug("[过滤] 未限制新闻类型，%d 条全部保留", len(news_list))
        return news_list

    filtered = []
    skipped = Counter()
    for news in news_list:
        # manifest 来源在发现阶段已完成版本分类，标题中未必含可识别关键词
        ntype = news.get("_version_type") or classify_news_type(news['title'])
        # "other" 类型不受过滤控制（始终保留或跳过取决于配置）
        if ntype == "other":
            if news_types.get("other", True):
                filtered.append(news)
            else:
                skipped[ntype] += 1
            continue
        if news_types.get(ntype, True):
            filtered.append(news)
        else:
            skipped[ntype] += 1

    detail = "，".join(f"{k} {v} 条" for k, v in skipped.items()) or "无"
    log_info("[过滤] 类型过滤: %d/%d 条保留（被过滤: %s）", len(filtered), len(news_list), detail)
    return filtered


def load_state(state_file: str) -> dict:
    """加载处理状态"""
    if os.path.exists(state_file):
        try:
            with open(state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            log_warning("状态文件 %s 读取失败，将重置为初始状态", state_file, exc_info=True)
    return {"posted_urls": [], "last_run": None, "_first_run": True}


def save_state(state_file: str, state: dict):
    """保存处理状态"""
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 确保目录存在
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    # 原子写：写盘期间被 kill 不会留下半截 JSON，否则下次运行按「首次运行」处理，
    # 把当前新闻全部记成已处理（一篇都没发布，见 docs/USAGE.md 的 Actions 一节）
    save_json_atomic(state_file, state, ".state-")
    log_debug("[状态] 已写入 %s（已处理 %d 条）", state_file, len(state.get("posted_urls", [])))


def _fetch_all_news(config: dict) -> list:
    """获取所有来源的新闻（搜索 API + Java 版本清单 + Feedback），合并去重返回"""
    all_news = []
    seen_urls = set()

    def _add(items, source):
        """按 URL 去重后并入结果，返回实际新增条数"""
        added = 0
        for news in items:
            url = news.get('url', '')
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            news['_source'] = source
            all_news.append(news)
            added += 1
        return added

    page_size = config.get("minecraft_api", {}).get("pageSize", 10)
    api_started = time.monotonic()
    api_news = get_latest_news_list(page_size=page_size, config=config)
    api_added = _add(api_news, 'minecraft_api')
    log_info("[主] 来源 minecraft_api: 新并入 %d 条（原始 %d 条，用时 %s）",
             api_added, len(api_news), elapsed_since(api_started))

    # Java 版本清单：搜索 API 的 sortType 按索引时间排序，可能漏掉新版本日志，
    # 这里用 Mojang 版本清单补齐（API 已返回同一篇文章时保留 API 版本，元数据更全）
    if config.get("version_manifest", {}).get("enabled", True):
        manifest_started = time.monotonic()
        manifest_news = get_java_news_from_manifest(config=config)
        manifest_added = _add(manifest_news, 'version_manifest')
        log_info("[主] 来源 version_manifest: 新并入 %d 条（原始 %d 条，用时 %s）",
                 manifest_added, len(manifest_news), elapsed_since(manifest_started))
    else:
        log_debug("[主] version_manifest 未启用，跳过")

    feedback_config = config.get('feedback_site', {})
    if feedback_config.get('enabled', False):
        feedback_started = time.monotonic()
        try:
            scraper = FeedbackScraper(config)
            feedback_sections = scraper.get_latest_articles()
            feedback_total = 0
            detail = []
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
                    feedback_total += 1
                detail.append(
                    f"{section_data.get('name_cn') or section_name} {len(section_data['articles'])} 条"
                )
            log_info("[主] 来源 feedback: %d 条（%s，用时 %s）",
                     feedback_total, "，".join(detail) or "无栏目", elapsed_since(feedback_started))
        except ImportError as e:
            log_warning("[主] Feedback 不可用（curl_cffi 未安装？）: %s", e)
        except Exception as e:
            log_error("[主] Feedback 获取失败: %s", e, exc_info=True)
    else:
        log_debug("[主] feedback_site 未启用，跳过")

    log_info("[主] 多来源合并去重后共 %d 条候选", len(all_news))
    return all_news


def _filter_and_check_state(all_news: list, config: dict, state_file: str,
                            dry_run: bool = False):
    """
    类型过滤 + 加载状态 + 首次运行检测 + 过滤已处理。

    `dry_run=True` 时只报告「真跑会怎么处理」，绝不写状态：首次运行的基线（把现有新闻
    标记为已处理、把 output 里已有的产物标记为已发布）一旦在 dry-run 里落盘，用户按
    README 的顺序「先 --dry-run 再真跑」时，真跑就什么都做不了了。

    Returns:
        (new_news, state, posted_urls) 或 None（首次运行/无新内容时）
    """
    api_items = [n for n in all_news if n.get('_source') == 'minecraft_api']
    manifest_items = [n for n in all_news if n.get('_source') == 'version_manifest']
    feedback_items = [n for n in all_news if n.get('_source') == 'feedback']
    # 版本清单候选和 API 候选一起过类型过滤：manifest 来源在发现阶段已带
    # _version_type，不走标题关键词猜测。漏掉这一类会让「版本清单补齐 Java
    # 版本日志」整条链路空转（候选只出现在来源统计里，永远进不了待处理列表）。
    filtered_api = filter_news_by_types(api_items + manifest_items, config)
    filtered = filtered_api + feedback_items
    log_debug("[主] 参与状态比对的候选: %d 条（API+Manifest %d + feedback %d）",
              len(filtered), len(filtered_api), len(feedback_items))

    state = load_state(state_file)
    posted_urls = set(state.get("posted_urls", []))

    if state.get("_first_run", False):
        if dry_run:
            log_info("[主] 检测到首次运行：真跑会把当前 %d 条新闻标记为已处理（不发布历史内容）；"
                     "本次 --dry-run 不写状态", len(filtered))
            return None
        log_info("[主] 检测到首次运行，将当前 %d 条新闻标记为已处理（不发布历史内容）", len(filtered))
        posted_urls.update(n['url'] for n in filtered)
        state["posted_urls"] = list(posted_urls)
        state.pop("_first_run", None)
        save_state(state_file, state)

        # 同时将 output 目录中已有的文件标记为已发布，避免重复发布
        save_dir = config["output"]["save_dir"]
        poster_state_file = os.path.join(save_dir, ".posted.json")
        if os.path.exists(save_dir):
            from poster import load_posted, save_posted
            existing_posted = load_posted(poster_state_file)
            existing_stems = {
                os.path.splitext(os.path.basename(p))[0]
                for p in glob.glob(os.path.join(save_dir, "*.txt"))
            }
            if existing_stems:
                existing_posted.update(existing_stems)
                save_posted(poster_state_file, existing_posted)
                log_info("[主] 已将 output 目录中 %d 个文件标记为已发布", len(existing_stems))

        return None

    new_news = [n for n in filtered if n['url'] not in posted_urls]
    skipped = len(filtered) - len(new_news)
    log_info("[主] 状态检查: 候选 %d 条，已处理 %d 条，本次待处理 %d 条",
             len(filtered), skipped, len(new_news))
    return new_news, state, posted_urls


def _news_type_of(news: dict) -> str:
    """给出新闻的类型标签（仅用于日志展示）"""
    source = news.get('_source', 'minecraft_api')
    if source == 'minecraft_api':
        return classify_news_type(news.get('title', ''))
    if source == 'version_manifest':
        return news.get('_version_type', 'java_snapshot')
    return 'feedback'


def _summarize_blocks(blocks: list) -> dict:
    """统计内容块与译文覆盖情况，用于日志。"""
    type_counter = Counter()
    text_blocks = 0
    translated_blocks = 0
    source_chars = 0
    translated_chars = 0

    for block in blocks or []:
        type_counter[block.get("type") or "?"] += 1
        if block.get("type") in ("img", "pre"):
            continue
        source_text = (block.get("source_text") or "").strip()
        if not source_text:
            continue
        text_blocks += 1
        source_chars += len(source_text)
        translated_text = (block.get("translated_text") or "").strip()
        if translated_text:
            translated_blocks += 1
            translated_chars += len(translated_text)

    return {
        "total": len(blocks or []),
        "types": type_counter,
        "text_blocks": text_blocks,
        "translated_blocks": translated_blocks,
        "source_chars": source_chars,
        "translated_chars": translated_chars,
    }


def _process_single_article(news: dict, config: dict, save_dir: str, modules_cfg) -> tuple | None:
    """
    处理单篇文章：解析 → 翻译 → 保存 JSON → 下载头图 → 转换。

    Returns:
        (stem, bbcode_path, json_path) 或 None
    """
    article_started = time.monotonic()
    source = news.get('_source', 'minecraft_api')

    full_data = process_feedback_news(news, config) if source == 'feedback' else process_article(news, config=config)
    if not full_data:
        log_error("[主] 文章解析/翻译失败，跳过: %s", news.get('url', ''))
        return None

    log_info("[主] 标题: 原文「%s」", full_data.get("title", ""))
    log_info("[主] 标题: 译文「%s」", full_data.get("translated_title") or "（未翻译，将按原文发布）")
    if full_data.get("section_cn"):
        log_info("[主] 栏目: %s", full_data["section_cn"])

    stats = _summarize_blocks(full_data.get("blocks"))
    type_detail = "，".join(f"{k} {v}" for k, v in stats["types"].most_common())
    log_info("[主] 内容块: 共 %d 个（%s）", stats["total"], type_detail or "空")
    if stats["text_blocks"]:
        coverage = stats["translated_blocks"] / stats["text_blocks"] * 100
        log_info("[主] 翻译覆盖: %d/%d 块（%.0f%%），原文 %d 字符 → 译文 %d 字符",
                 stats["translated_blocks"], stats["text_blocks"], coverage,
                 stats["source_chars"], stats["translated_chars"])
        if stats["translated_blocks"] < stats["text_blocks"]:
            log_warning("[主] 有 %d 个文本块未取到译文，将保留原文",
                        stats["text_blocks"] - stats["translated_blocks"])

    json_started = time.monotonic()
    json_path = save_article_json(full_data, save_dir=save_dir, config=config)
    if not json_path:
        log_error("[主] JSON 保存失败，跳过: %s", news.get('url', ''))
        return None
    log_debug("[主] JSON 保存用时 %s", elapsed_since(json_started))

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
            log_debug("图片扩展名解析失败，使用默认 .jpg", exc_info=True)
        base_path = json_path.rsplit(".", 1)[0]
        image_path = base_path + image_ext
        image_started = time.monotonic()
        if download_header_image(header_image_url, image_path, config=config):
            log_info("[主] 头图已保存: %s（%s，用时 %s）",
                     image_path, human_size(_safe_size(image_path)), elapsed_since(image_started))
        else:
            log_warning("[主] 头图下载失败，将无图发布: %s", header_image_url)
    else:
        log_debug("[主] 文章无头图，跳过图片下载")

    base_path = json_path.rsplit(".", 1)[0]
    convert_started = time.monotonic()
    try:
        bbcode_path, md_path = convert_json_file(
            json_path, output_prefix=base_path, modules_config=modules_cfg
        )
    except Exception as e:
        log_error("[主] 转换 BBCode/Markdown 失败: %s", e, exc_info=True)
        return None
    log_info("[主] 已生成 BBCode: %s（%s）", bbcode_path, human_size(_safe_size(bbcode_path)))
    if md_path:
        log_info("[主] 已生成 Markdown: %s（%s）", md_path, human_size(_safe_size(md_path)))

    # 专栏正文只在启用专栏发布时生成：没启用就没必要在 output 里多留一份产物。
    # 产物按 bilibili.renderer 二选一：html（老接口）/ opus（新版编辑器段模型）。
    if (config.get("bilibili") or {}).get("enabled", False):
        renderer = _bili_renderer(config)
        try:
            content_path = _convert_bili_content(json_path, base_path, modules_cfg, renderer)
            log_info("[主] 已生成专栏正文（%s）: %s（%s）",
                     renderer, content_path, human_size(_safe_size(content_path)))
        except Exception:  # noqa: BLE001 - 少了正文只是不能投专栏，不该中断整篇处理
            log_error("[主] 生成专栏正文失败，本次不投专栏: %s", json_path, exc_info=True)
    log_debug("[主] 转换用时 %s", elapsed_since(convert_started))

    stem = os.path.basename(base_path)
    log_info("[主] 单篇处理完成: %s（总用时 %s）", stem, elapsed_since(article_started))
    return stem, bbcode_path, json_path


def _safe_size(path: str) -> int | None:
    """取文件大小，失败返回 None（日志用，不应抛异常）。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _load_modules_config() -> dict | None:
    """读取 config/modules_config.json；缺失时返回 None，转换器会退回内置默认模块。"""
    modules_cfg_path = os.path.join(CONFIG_DIR, "modules_config.json")
    if os.path.isfile(modules_cfg_path):
        with open(modules_cfg_path, encoding="utf-8") as f:
            log_debug("[主] 已加载模块配置: %s", modules_cfg_path)
            return json.load(f)
    log_debug("[主] 未找到模块配置，将使用内置默认模块: %s", modules_cfg_path)
    return None


def run_scrape(config: dict, state_file: str, dry_run: bool = False, stats: dict = None) -> list:
    """
    执行爬取流程：获取新闻 → 过滤类型 → 检查状态 → 翻译 → 保存

    Args:
        stats: 可选，运行统计字典（写入 found/new/processed 供上层汇总）

    Returns:
        已处理的文章 (stem, txt_path, json_path) 列表
    """
    started = time.monotonic()
    save_dir = config["output"]["save_dir"]
    os.makedirs(save_dir, exist_ok=True)

    all_news = _fetch_all_news(config)
    if stats is not None:
        stats["found"] = len(all_news)
    if not all_news:
        log_warning("[主] 所有来源都没有取到新闻，本次结束")
        return []

    result = _filter_and_check_state(all_news, config, state_file, dry_run=dry_run)
    if result is None:
        return []
    new_news, state, posted_urls = result
    if stats is not None:
        stats["new"] = len(new_news)

    if not new_news:
        log_info("[主] 没有新新闻（候选 %d 条均已处理过），用时 %s",
                 len(all_news), elapsed_since(started))
        return []

    log_info("[主] 发现 %d 条新新闻待处理", len(new_news))

    if dry_run:
        log_info("[Dry Run] 新新闻列表（不会翻译/保存/发布）：")
        for i, news in enumerate(new_news, 1):
            log_info("[Dry Run] %d. [%s][%s] %s", i, news.get('_source', 'minecraft_api'),
                     _news_type_of(news), news.get('title', ''))
            log_info("[Dry Run]     %s", news.get('url', ''))
        return []

    modules_cfg = _load_modules_config()

    # 处理失败的 URL 累计失败次数，超过上限才放弃，避免"失败即永久跳过"
    failed_attempts = dict(state.get("failed_attempts", {}))
    max_attempts = max(int(config.get("retry", {}).get("max_article_attempts", 3)), 1)

    processed = []
    failed = 0
    for i, news in enumerate(new_news, 1):
        source = news.get('_source', 'minecraft_api')
        url = news['url']
        article_started = time.monotonic()

        log_section(f"[{i}/{len(new_news)}] {news.get('title', '')}")
        log_info("[主] 来源: %s | 类型: %s | 链接: %s", source, _news_type_of(news), url)

        try:
            item = _process_single_article(news, config, save_dir, modules_cfg)
        except Exception as e:
            log_error("[主] 处理异常: %s", e, exc_info=True)
            item = None

        if item:
            posted_urls.add(url)
            failed_attempts.pop(url, None)
            log_info("[主] ✓ 处理成功: %s（用时 %s）", item[0], elapsed_since(article_started))
        else:
            failed += 1
            attempts = failed_attempts.get(url, 0) + 1
            if attempts >= max_attempts:
                log_error("[主] 已连续失败 %d 次，放弃该文章: %s", attempts, url)
                posted_urls.add(url)
                failed_attempts.pop(url, None)
            else:
                failed_attempts[url] = attempts
                log_warning("[主] 处理失败（第 %d/%d 次），下次运行将重试: %s",
                            attempts, max_attempts, url)

        state["posted_urls"] = list(posted_urls)
        state["failed_attempts"] = failed_attempts
        save_state(state_file, state)
        if item:
            processed.append(item)

    if stats is not None:
        stats["processed"] = len(processed)
    log_info("[主] 爬取阶段完成: 待处理 %d 条，成功 %d 条，失败 %d 条，用时 %s",
             len(new_news), len(processed), failed, elapsed_since(started))
    return processed


def _backfill_enabled(config: dict) -> bool:
    """是否补发 output 里已生成但没发出去的文章（retry.backfill_unposted）。"""
    return bool((config.get("retry") or {}).get("backfill_unposted", True))


def _collect_unposted(save_dir: str, posted: set, exclude: set | None = None) -> list:
    """收集 output 里「.txt + 同名 .json」且没发布过的文章，按文件名排序（旧→新）。

    post_news_file 要求 BBCode 与元数据成对存在，缺一个就发不了，所以这里一并过滤。
    """
    skip = exclude or set()
    items = []
    for txt_path in sorted(glob.glob(os.path.join(save_dir, "*.txt"))):
        stem = os.path.splitext(os.path.basename(txt_path))[0]
        if stem in posted or stem in skip:
            continue
        json_path = os.path.join(save_dir, stem + ".json")
        if os.path.isfile(json_path):
            items.append((stem, txt_path, json_path))
    return items


def _has_unposted_mcbbs(config: dict) -> bool:
    """没有新文章时是否还有该补发的 MCBBS 文章；main 据此决定要不要跑发布。"""
    if not (config.get("mcbbs") or {}).get("enabled", False) or not _backfill_enabled(config):
        return False
    save_dir = config["output"]["save_dir"]
    posted = load_posted(os.path.join(save_dir, ".posted.json"))
    return bool(_collect_unposted(save_dir, posted))


def _pending_highlights(save_dir: str) -> dict:
    """待补高亮的记录：发帖登记过、但还没设上色的帖子（含还在审核的）。"""
    records = load_thread_records(thread_records_path(save_dir))
    return {stem: rec for stem, rec in records.items() if rec.get("state") == "pending"}


def _has_pending_mcbbs_highlights(config: dict) -> bool:
    """没有新文章时，是否还有待补高亮的帖子要处理。"""
    if not (config.get("mcbbs") or {}).get("enabled", False):
        return False
    return bool(_pending_highlights(config["output"]["save_dir"]))


def _sweep_highlights(poster, save_dir: str) -> dict:
    """处理待补高亮的帖子。补高亮失败只记日志：它不该拖累本轮发帖。"""
    try:
        return poster.process_pending_highlights(thread_records_path(save_dir))
    except Exception as e:  # noqa: BLE001 - 兜底，任何异常都要留在日志里
        log_warning("[发布] 补高亮流程出错（不影响发帖）: %s", e, exc_info=True)
        return {}


def _build_publish_queue(processed: list, save_dir: str, posted: set, config: dict) -> list:
    """本轮要发布的文章 = 本轮新处理的 + 历史未发布的（受 retry.backfill_unposted 控制）。"""
    queue = list(processed)
    unposted = _collect_unposted(save_dir, posted, {stem for stem, _, _ in queue})
    if not unposted:
        return queue
    if not _backfill_enabled(config):
        log_warning("[发布] 有 %d 篇已生成但未发布的文章，补发未开启"
                    "（config.json 的 retry.backfill_unposted = false）", len(unposted))
        return queue
    log_info("[发布] 补发 %d 篇已生成但未发布的文章", len(unposted))
    for stem, _txt_path, _json_path in unposted:
        log_info("[发布] 补发: %s", stem)
    queue.extend(unposted)
    return queue


def run_post(processed: list, config: dict, no_image: bool = False, no_json: bool = False,
             config_path: str | None = None) -> dict:
    """执行发布流程

    Returns:
        {"success": int, "failed": int, "urls": [发帖地址...]}
    """
    from poster import MCBBSPoster, load_posted, load_poster_config, save_posted

    result = {"success": 0, "failed": 0, "urls": []}
    started = time.monotonic()

    if not config.get("mcbbs", {}).get("enabled", False):
        log_warning("[发布] MCBBS 发布未启用（config.json 中 mcbbs.enabled = false），跳过发帖")
        return result

    mcbbs_config = load_poster_config(config_path)
    save_dir = config["output"]["save_dir"]
    poster_state_file = os.path.join(save_dir, ".posted.json")
    posted = load_posted(poster_state_file)

    queue = _build_publish_queue(processed, save_dir, posted, config)
    pending_highlights = _pending_highlights(save_dir)
    if not queue and not pending_highlights:
        log_info("[发布] 没有需要发布的文章")
        return result

    poster = MCBBSPoster(mcbbs_config)
    # 补高亮放在发帖之前：它只在确认过审后才登录，登录失败也不该拖累本轮发帖
    _sweep_highlights(poster, save_dir)
    if not queue:
        log_info("[发布] 本轮没有新帖，只处理了待补高亮")
        return result

    log_info("[发布] 目标站点: %s | 版块 fid=%s | 待发布 %d 篇",
             mcbbs_config.get("base_url"), mcbbs_config.get("forum_fid"), len(queue))

    login_started = time.monotonic()
    try:
        poster.login()
    except Exception as e:
        log_error("[发布] MCBBS 登录失败，本次不发布: %s", e, exc_info=True)
        result["failed"] = len(queue)
        return result
    log_info("[发布] 登录成功，用时 %s", elapsed_since(login_started))

    for index, (stem, txt_path, json_path) in enumerate(queue, 1):
        post_started = time.monotonic()
        log_info("[发布] %d/%d 开始发帖: %s", index, len(queue), stem)
        try:
            url = poster.post_news_file(stem, txt_path, json_path, save_dir,
                                        no_image=no_image, attach_json=not no_json)
            posted.add(stem)
            save_posted(poster_state_file, posted)
            result["success"] += 1
            result["urls"].append(url)
            log_info("[发布] ✓ 发帖成功: %s（用时 %s）", url, elapsed_since(post_started))
            time.sleep(3)  # 发帖间隔，避免被封
        except Exception as e:
            result["failed"] += 1
            log_error("[发布] ✗ 发帖失败: %s | %s（用时 %s）",
                      stem, e, elapsed_since(post_started), exc_info=True)

    log_info("[发布] 发布完成: 成功 %d，失败 %d，用时 %s",
             result["success"], result["failed"], elapsed_since(started))
    for url in result["urls"]:
        log_info("[发布] 帖子地址: %s", url)
    return result


def run_post_only(config: dict, config_path: str | None = None) -> dict:
    """仅发布 output 目录中未发布的文件"""
    from poster import MCBBSPoster, load_posted, load_poster_config, save_posted

    result = {"success": 0, "failed": 0, "urls": []}
    started = time.monotonic()

    mcbbs_config = load_poster_config(config_path)
    if not config.get("mcbbs", {}).get("enabled", False):
        log_warning("[发布] MCBBS 发布未启用（config.json 中 mcbbs.enabled = false）")
        return result

    save_dir = config["output"]["save_dir"]

    all_news = _collect_unposted(save_dir, set())
    pending_highlights = _pending_highlights(save_dir)

    # 没有新帖但有待补高亮时也要往下走：高亮补做只需要登录，不发帖
    if not all_news and not pending_highlights:
        log_warning("[发布] output 目录没有可发布的新闻文件（需要 .txt + 同名 .json）: %s", save_dir)
        return result

    # 使用 poster 的状态文件
    poster_state_file = os.path.join(save_dir, ".posted.json")
    posted = load_posted(poster_state_file)
    pending = [(s, t, j) for s, t, j in all_news if s not in posted]

    if not pending and not pending_highlights:
        log_info("[发布] 所有 %d 个文件已发布过，无需处理", len(all_news))
        return result

    poster = MCBBSPoster(mcbbs_config)
    _sweep_highlights(poster, save_dir)
    if not pending:
        log_info("[发布] 本轮没有待发布的文件，只处理了待补高亮")
        return result

    log_info("[发布] 待发布: %d 个（共 %d 个文件，已发布 %d 个）",
             len(pending), len(all_news), len(all_news) - len(pending))

    login_started = time.monotonic()
    try:
        poster.login()
    except Exception as e:
        log_error("[发布] MCBBS 登录失败，本次不发布: %s", e, exc_info=True)
        result["failed"] = len(pending)
        return result
    log_info("[发布] 登录成功，用时 %s", elapsed_since(login_started))

    for index, (stem, txt_path, json_path) in enumerate(pending, 1):
        post_started = time.monotonic()
        log_info("[发布] %d/%d 开始发帖: %s", index, len(pending), stem)
        try:
            url = poster.post_news_file(stem, txt_path, json_path, save_dir)
            posted.add(stem)
            save_posted(poster_state_file, posted)
            result["success"] += 1
            result["urls"].append(url)
            log_info("[发布] ✓ 发帖成功: %s（用时 %s）", url, elapsed_since(post_started))
            time.sleep(3)
        except Exception as e:
            result["failed"] += 1
            log_error("[发布] ✗ 发帖失败: %s | %s（用时 %s）",
                      stem, e, elapsed_since(post_started), exc_info=True)

    log_info("[发布] 发布完成: 成功 %d，失败 %d，用时 %s",
             result["success"], result["failed"], elapsed_since(started))
    for url in result["urls"]:
        log_info("[发布] 帖子地址: %s", url)
    return result


# ── B站专栏发布 ──────────────────────────────────────

def _bili_state_file(save_dir: str) -> str:
    """专栏的已投稿记录。与 MCBBS 的 .posted.json 分开，语义互不干扰。"""
    return os.path.join(save_dir, ".posted_bili.json")


# 段模型产物的后缀：news_xxx.opus.json 也是 news_*.json 的形状，扫文章时必须跳过。
# 不跳的话它会被当成一篇「标题叫 xxx.opus」的文章排进队列：先白转换一轮，再以
# 「找不到标题」失败，还会往已投稿记录里塞一条假记录。
_BILI_OPUS_SUFFIX = ".opus.json"

# 投稿必需的两块 cookie：SESSDATA=登录态，bili_jct=csrf。都缺就别开跑。
_BILI_REQUIRED_COOKIES = ("SESSDATA", "bili_jct")


def _bili_news_items(save_dir: str) -> list:
    """output 里可投稿的文章：[(stem, txt 路径, json 路径)]。"""
    items = []
    for json_path in sorted(glob.glob(os.path.join(save_dir, "news_*.json"))):
        name = os.path.basename(json_path)
        if name.endswith(_BILI_OPUS_SUFFIX):
            continue
        stem = os.path.splitext(name)[0]
        items.append((stem, os.path.join(save_dir, stem + ".txt"), json_path))
    return items


def _seed_bili_posted(save_dir: str, state_file: str, processed: list) -> set:
    """首次启用专栏时，把 output 里已有的文章记成「已投稿」当作基线。

    没有这一步，开关一开就会把积压的历史整批投出去（尤其是 --post-only：它没有
    「本次新处理」的概念，pending 就是全部历史）。做基线时排除本次新处理的篇目，
    否则自己刚生成的内容会被自己跳过。已投稿记录非空就不再重做；要重做基线（比如
    升级后想重新清空历史）把 state 文件删掉、或清空成 `[]` 再跑一次即可。
    """
    posted = load_posted(state_file)
    if posted:
        return posted

    current = {stem for stem, *_ in processed}
    existing = {item[0] for item in _bili_news_items(save_dir)} - current
    if not existing:
        return posted

    posted |= existing
    save_posted(state_file, posted)
    log_info("[专栏] 首次启用：把 output 里已有的 %d 篇文章标记为已投稿（不补发历史）；"
             "要补投历史请先删掉 %s，再用 --post-only --bili-backfill 跑一次",
             len(existing), os.path.basename(state_file))
    return posted


def _bili_renderer(config: dict) -> str:
    """专栏正文模型：html（老接口）| opus（新版编辑器段模型）。取值非法时按 html 处理。"""
    value = str((config.get("bilibili") or {}).get("renderer") or "html").strip().lower()
    return value if value in ("html", "opus") else "html"


def _bili_content_suffix(renderer: str) -> str:
    """两种正文模型的产物后缀：HTML 是 .html，段模型是 .opus.json。"""
    return _BILI_OPUS_SUFFIX if renderer == "opus" else ".html"


def _convert_bili_content(json_path: str, output_prefix: str, modules_cfg, renderer: str) -> str:
    """按正文模型生成专栏产物（HTML 或段模型 JSON）。"""
    if renderer == "opus":
        return convert_bili_opus_file(json_path, output_prefix=output_prefix, modules_config=modules_cfg)
    return convert_bili_html_file(json_path, output_prefix=output_prefix, modules_config=modules_cfg)


def _bili_artifact_stale(content_path: str, json_path: str) -> bool:
    """产物是否比文章 JSON 旧。取不到时间戳时按「不旧」处理（保持旧行为）。"""
    try:
        return os.path.getmtime(content_path) < os.path.getmtime(json_path)
    except OSError:
        return False


def _ensure_bili_content(json_path: str, content_path: str, modules_cfg, renderer: str) -> str | None:
    """专栏正文产物缺失、或比 JSON 旧时，按 JSON 现场生成。

    历史产物（专栏功能上线前抓的文章）没有这份文件，而 --scrape-only 只处理「新新闻」，
    让用户为了补一份产物去重跑抓取是死路，所以这里按需补生成。

    「比 JSON 旧」也重建：重译过、改过标题的文章，产物不会自己更新，不重渲就会把旧正文
    投出去。改 modules_config.json 不触发重建（模板是全局配置，给每篇产物存指纹不值得），
    想强制重渲删掉产物即可。
    """
    if os.path.isfile(content_path):
        if not _bili_artifact_stale(content_path, json_path):
            return content_path
        log_info("[专栏] 专栏正文比文章 JSON 旧，重新生成: %s", os.path.basename(content_path))
    else:
        log_info("[专栏] 缺少专栏正文（%s），按 JSON 现场生成: %s",
                 renderer, os.path.basename(content_path))
    stem = content_path[: -len(_bili_content_suffix(renderer))]
    try:
        return _convert_bili_content(json_path, stem, modules_cfg, renderer)
    except Exception:  # noqa: BLE001 - 单篇转换失败按单篇失败处理，不中断整批
        log_error("[专栏] 生成专栏正文失败: %s", json_path, exc_info=True)
        return None


def _publish_bili_queue(poster, queue: list, config: dict, posted: set) -> dict:
    """逐篇投稿（两个入口共用的循环）：调用方负责账号校验与队列筛选。"""
    result = {"success": 0, "failed": 0, "pending_review": 0, "urls": [], "drafts": []}
    save_dir = config["output"]["save_dir"]
    state_file = _bili_state_file(save_dir)
    interval = float((config.get("bilibili") or {}).get("interval_seconds") or 0)
    modules_cfg = _load_modules_config()
    renderer = _bili_renderer(config)
    suffix = _bili_content_suffix(renderer)

    for index, (stem, _txt_path, json_path) in enumerate(queue, 1):
        item_started = time.monotonic()
        log_info("[专栏] %d/%d 开始投稿: %s（%s）", index, len(queue), stem, renderer)

        content_path = _ensure_bili_content(
            json_path, os.path.join(save_dir, stem + suffix), modules_cfg, renderer
        )
        if not content_path:
            result["failed"] += 1
            continue

        try:
            outcome = poster.post_news_file(
                stem, json_path, content_path, cover_path=find_image(save_dir, stem)
            )
        except Exception as e:  # noqa: BLE001 - 单篇异常不该打断整批投稿
            result["failed"] += 1
            log_error("[专栏] ✗ 投稿异常: %s | %s（用时 %s）",
                      stem, e, elapsed_since(item_started), exc_info=True)
            continue

        if not outcome.get("ok"):
            result["failed"] += 1
            log_error("[专栏] ✗ 投稿失败: %s | %s", stem, outcome.get("error") or "未知错误")
            continue

        result["success"] += 1
        if outcome.get("pending_review"):
            result["pending_review"] += 1
        if outcome.get("url"):
            result["urls"].append(outcome["url"])
        elif outcome.get("edit_url"):
            result["drafts"].append(outcome["edit_url"])
        posted.add(stem)
        save_posted(state_file, posted)
        log_info("[专栏] ✓ 完成: %s（%s，用时 %s）", stem,
                 outcome.get("state_desc") or "已保存草稿", elapsed_since(item_started))

        if index < len(queue) and interval > 0:
            log_debug("[专栏] 间隔 %.0f 秒后投下一篇", interval)
            time.sleep(interval)

    return result


def _check_bili_login(poster) -> bool:
    """校验专栏登录态。凭据失效/风控/网络问题都在这里收口，由调用方记账。"""
    try:
        poster.check_login()
        return True
    except Exception as e:  # noqa: BLE001 - 没有 cookie、风控、网络三种都算「这批投不了」
        log_error("[专栏] 登录校验失败，本次不投稿: %s", e, exc_info=True)
        return False


def _bili_cookie_missing(bili_cfg: dict) -> list:
    """缺哪些登录 cookie（空列表表示可以投稿）。

    取值口径与 bili.poster.load_cookies 一致：环境变量优先，环境变量为空则回落到
    cookie 文件；两者都没有就是没配置。没配置时直接跳过专栏——把它当失败来记，
    每次运行都会刷一屏错误，而用户其实只是还没填 cookie。
    """
    from bili.poster import load_cookies

    cookies = load_cookies(bili_cfg)
    return [name for name in _BILI_REQUIRED_COOKIES if not cookies.get(name)]


def _log_missing_bili_cookies(missing: list) -> None:
    """凭据没配就整段跳过专栏：这不是失败，只是还没配。"""
    log_warning("[专栏] 没有 B站 登录 cookie（缺 %s），本次跳过专栏："
                "把 SESSDATA / bili_jct 写进 config/bili_cookies.json，"
                "或设环境变量 BILI_SESSDATA / BILI_JCT，或用 tools/bili_login.py 扫码生成",
                "、".join(missing))


def _bili_skip_reason(json_path: str, bili_cfg: dict) -> str | None:
    """专栏投放前的来源 / 类型过滤，返回跳过原因（None 表示可以投）。

    判断依据是存下来的 JSON：`source` 是抓取来源（minecraft_api / version_manifest /
    feedback），类型从原文标题推（与抓取阶段 classify_news_type 同一口径）。
    两个名单都为空的配置项表示「不过滤」——老配置没写这两项时行为与以前一致。
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        # 能解析但不是对象：多半是 .opus.json 这类产物，不是文章元数据。
        # 不判形状的话 data.get 会抛 AttributeError，把整批投稿带崩。
        return "元数据不是 JSON 对象（可能是专栏产物）"

    source = str(data.get("source") or "").strip()
    excluded = {str(item).strip().lower() for item in (bili_cfg.get("exclude_sources") or []) if str(item).strip()}
    if source and source.lower() in excluded:
        return f"来源 {source} 在 exclude_sources 里"

    allowed = {str(item).strip().lower() for item in (bili_cfg.get("publish_types") or []) if str(item).strip()}
    if allowed:
        news_type = (classify_article_type(data.get("title") or "", fallback="other") or "other").lower()
        if news_type not in allowed:
            return f"类型 {news_type} 不在 publish_types 里"
    return None


def _filter_bili_queue(pending: list, bili_cfg: dict) -> list:
    """按配置筛掉不投专栏的文章（快讯、feedback 等），并打日志说明为什么跳过。"""
    if not (bili_cfg.get("publish_types") or bili_cfg.get("exclude_sources")):
        return pending

    kept = []
    skipped = 0
    for item in pending:
        try:
            reason = _bili_skip_reason(item[2], bili_cfg)
        except (OSError, json.JSONDecodeError) as e:
            # 读不出元数据就不替用户做决定：照旧投，失败会由投稿阶段报错
            log_debug("[专栏] 读取元数据失败，按可投处理: %s（%s）", item[2], e)
            kept.append(item)
            continue
        if reason is None:
            kept.append(item)
        else:
            skipped += 1
            log_info("[专栏] 跳过 %s：%s", item[0], reason)
    if skipped:
        log_info("[专栏] 按配置跳过 %d 篇（publish_types %s / exclude_sources %s）",
                 skipped, bili_cfg.get("publish_types"), bili_cfg.get("exclude_sources"))
    return kept


def _bili_cap_queue(pending: list, bili_cfg: dict) -> list:
    """按 bilibili.max_per_run 截断本次要投的篇目（两个入口同一口径）。

    全流程模式只把「当批新文章」排进队列，被截掉的篇目下轮不会自动补上，
    要用 --post-only 补投——日志里说清楚，别让人以为下次运行会接着投。
    """
    max_per_run = int(bili_cfg.get("max_per_run") or 0)
    if max_per_run <= 0 or len(pending) <= max_per_run:
        return pending
    log_info("[专栏] 本次限投 %d 篇，其余 %d 篇本次不投（bilibili.max_per_run；"
             "全流程模式只投当批新文章，可再用 --post-only 补投）",
             max_per_run, len(pending) - max_per_run)
    return pending[:max_per_run]


def run_post_bili(processed: list, config: dict) -> dict:
    """把本次处理好的文章投稿到 B站专栏。

    Returns:
        {"success","failed","pending_review","urls","drafts"}
    """
    from bili.poster import BilibiliPoster

    result = {"success": 0, "failed": 0, "pending_review": 0, "urls": [], "drafts": []}
    bili_cfg = config.get("bilibili") or {}

    if not bili_cfg.get("enabled", False):
        log_warning("[专栏] 未启用（config.json 里 bilibili.enabled = false），跳过")
        return result

    started = time.monotonic()
    save_dir = config["output"]["save_dir"]

    missing = _bili_cookie_missing(bili_cfg)
    if missing:
        _log_missing_bili_cookies(missing)
        return result

    # 基线先记账：即使这一轮没抓到新文章也要把积压的历史标成已投稿，
    # 否则「升级后第一次运行」什么都没发生，下次真抓到新文章时才有基线
    posted = _seed_bili_posted(save_dir, _bili_state_file(save_dir), processed)

    if not processed:
        log_info("[专栏] 没有需要投稿的文章")
        return result

    pending = [item for item in processed if item[0] not in posted]
    if not pending:
        log_info("[专栏] %d 篇都已投稿过，无需处理", len(processed))
        return result

    pending = _filter_bili_queue(pending, bili_cfg)
    if not pending:
        log_info("[专栏] 本次没有符合投放条件的文章（按 publish_types / exclude_sources 过滤后为空）")
        return result

    queue = _bili_cap_queue(pending, bili_cfg)

    log_info("[专栏] 分区 %s | 提交方式 %s | 待投 %d 篇（间隔 %s 秒）",
             bili_cfg.get("category_id"), bili_cfg.get("submit"),
             len(queue), bili_cfg.get("interval_seconds"))

    poster = BilibiliPoster(bili_cfg, config.get("http") or {})
    if not _check_bili_login(poster):
        result["failed"] = len(queue)
        return result

    result = _publish_bili_queue(poster, queue, config, posted)
    log_info("[专栏] 发布完成: 成功 %d（其中待审核 %d），失败 %d，用时 %s",
             result["success"], result["pending_review"], result["failed"],
             elapsed_since(started))
    for url in result["urls"]:
        log_info("[专栏] 文章地址: %s", url)
    for edit_url in result["drafts"]:
        log_info("[专栏] 草稿待发布: %s", edit_url)
    return result


def run_post_only_bili(config: dict, backfill: bool = False) -> dict:
    """只投稿 output 目录里还没投稿过的文章（按 stem 记账）。

    首次运行没有已投稿记录时只做基线：把现有文章全部记为已投稿、一篇都不投（升级到带专栏
    的版本时不会把积压的历史整批刷出去）。要真补投历史：删掉 output/.posted_bili.json，
    再加 --bili-backfill 跑一次（backfill=True 跳过基线）。
    """
    from bili.poster import BilibiliPoster

    result = {"success": 0, "failed": 0, "pending_review": 0, "urls": [], "drafts": []}
    bili_cfg = config.get("bilibili") or {}

    if not bili_cfg.get("enabled", False):
        log_warning("[专栏] 未启用（config.json 里 bilibili.enabled = false）")
        return result

    save_dir = config["output"]["save_dir"]
    items = _bili_news_items(save_dir)
    if not items:
        log_warning("[专栏] output 目录没有可投稿的文章（需要 news_*.json）: %s", save_dir)
        return result

    missing = _bili_cookie_missing(bili_cfg)
    if missing:
        _log_missing_bili_cookies(missing)
        return result

    # --post-only 没有「本批新文章」的概念，所以这里做基线时把现有文章全部算作历史：
    # 首次运行（没有已投稿记录）只记账、一篇都不投，避免把积压的历史整批刷出去。
    if backfill:
        posted = load_posted(_bili_state_file(save_dir))
        log_info("[专栏] --bili-backfill：跳过首次基线，按已投稿记录补投剩余文章")
    else:
        posted = _seed_bili_posted(save_dir, _bili_state_file(save_dir), [])
    pending = [item for item in items if item[0] not in posted]
    if not pending:
        log_info("[专栏] 所有 %d 篇文章都投稿过，无需处理", len(items))
        return result

    pending = _filter_bili_queue(pending, bili_cfg)
    if not pending:
        log_info("[专栏] 本次没有符合投放条件的文章（按 publish_types / exclude_sources 过滤后为空）")
        return result

    started = time.monotonic()
    log_info("[专栏] 待投稿 %d 篇（共 %d 篇，已投稿 %d 篇）",
             len(pending), len(items), len(items) - len(pending))

    queue = _bili_cap_queue(pending, bili_cfg)

    poster = BilibiliPoster(bili_cfg, config.get("http") or {})
    if not _check_bili_login(poster):
        result["failed"] = len(queue)
        return result

    result = _publish_bili_queue(poster, queue, config, posted)
    log_info("[专栏] 发布完成: 成功 %d（其中待审核 %d），失败 %d，用时 %s",
             result["success"], result["pending_review"], result["failed"],
             elapsed_since(started))
    for url in result["urls"]:
        log_info("[专栏] 文章地址: %s", url)
    for edit_url in result["drafts"]:
        log_info("[专栏] 草稿待发布: %s", edit_url)
    return result


def _log_config_summary(config: dict, mode: str, save_dir: str, state_file: str):
    """把本次运行的关键配置写进日志（不打印任何密钥）。"""
    openai_cfg = config.get("openai_compat", {}) or {}
    news_types = config.get("news_types", {}) or {}
    enabled_types = [k for k, v in news_types.items() if v]
    feedback_cfg = config.get("feedback_site", {}) or {}
    feedback_sections = [
        s.get("name_cn") or s.get("name")
        for s in feedback_cfg.get("sections", [])
        if s.get("enabled", True) and (s.get("name_cn") or s.get("name"))
    ]

    log_info("[配置] 模式: %s | 输出目录: %s | 状态文件: %s", mode, save_dir, state_file)
    log_info("[配置] 翻译模型: %s @ %s（超时 %ss，max_tokens %s）",
             openai_cfg.get("model", "-"), openai_cfg.get("host", "-"),
             openai_cfg.get("timeout", "-"), openai_cfg.get("max_tokens", "-"))
    log_info("[配置] 启用类型: %s", ", ".join(enabled_types) if enabled_types else "全部")
    log_info("[配置] Java 版本清单补齐: %s",
             "启用" if (config.get("version_manifest", {}) or {}).get("enabled", True) else "禁用")
    log_info("[配置] Feedback 来源: %s",
             f"启用（{len(feedback_sections)} 个栏目: {', '.join(feedback_sections)}）"
             if feedback_cfg.get("enabled") else "禁用")
    log_info("[配置] MCBBS 发布: %s",
             "启用" if (config.get("mcbbs", {}) or {}).get("enabled", False) else "禁用")
    log_info("[配置] B站专栏: %s", _bili_config_summary(config))
    log_info("[配置] 日志文件: %s", current_log_path() or "-")


def _bili_config_summary(config: dict) -> str:
    """专栏配置的一句话摘要（只说 cookie 有没有，绝不打印值）。"""
    bili_cfg = config.get("bilibili") or {}
    if not bili_cfg.get("enabled", False):
        return "禁用"
    from bili.poster import load_cookies

    cookie_state = "已配置" if load_cookies(bili_cfg).get("SESSDATA") else "缺失（先跑 tools/bili_login.py）"
    return (f"启用（分区 {bili_cfg.get('category_id')}，提交方式 {bili_cfg.get('submit')}，"
            f"单次上限 {bili_cfg.get('max_per_run')}，cookie {cookie_state}）")


# ── 入口 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MCTTK — Minecraft 新闻自动爬取+翻译+发布",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py                    # 全流程自动运行\n"
            "  python main.py --dry-run          # 仅检测新新闻\n"
            "  python main.py --scrape-only      # 只爬取翻译，不发布\n"
            "  python main.py --post-only        # 只发布已翻译的文件\n"
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="仅检测新新闻，不实际处理")
    parser.add_argument("--scrape-only", action="store_true", help="只爬取+翻译+转换，不发布")
    parser.add_argument("--post-only", action="store_true", help="只发布 output 目录中未发布的文件")
    parser.add_argument("--no-image", action="store_true", help="发布时跳过图片上传（MCBBS）")
    parser.add_argument("--no-json", action="store_true", help="发布时跳过 JSON 附件上传（MCBBS）")
    parser.add_argument(
        "--publish", choices=("all", "mcbbs", "bilibili", "none"), default="all",
        help="发布目标：all=按各平台 enabled 开关发布（默认）；mcbbs/bilibili=只投其中一个；none=只生成不发布",
    )
    parser.add_argument(
        "--bili-backfill", action="store_true",
        help="配合 --post-only：跳过 B站专栏的首次基线，直接补投 output 里的历史文章",
    )
    parser.add_argument("--config", help="指定配置文件路径")
    args = parser.parse_args()

    # 设置输出编码
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

    # 初始化日志系统（级别/目录/保留天数可用环境变量覆盖，见 log_setup.py）
    setup_logging()

    if args.post_only:
        mode = "post-only"
    elif args.dry_run:
        mode = "dry-run"
    elif args.scrape_only:
        mode = "scrape-only"
    else:
        mode = "full"

    if args.bili_backfill and not args.post_only:
        log_warning("[主] --bili-backfill 只对 --post-only 生效，本次忽略")

    run_started = time.monotonic()
    log_section("MCTTK 运行开始")
    log_info("程序启动（模式: %s）", mode)

    # 加载配置
    if args.config:
        from scraper import load_config
        config = load_config(args.config)
    else:
        config = load_main_config()

    save_dir = config["output"]["save_dir"]
    state_file = os.path.join(save_dir, ".state.json")
    _log_config_summary(config, mode, save_dir, state_file)

    # 检查必要的 API 配置（非 post-only 模式需要）
    if not args.post_only:
        api_key = config.get("openai_compat", {}).get("api_key", "")
        host = config.get("openai_compat", {}).get("host", "")
        if not api_key or "example" in host:
            log_error("[配置] 未配置翻译 API（openai_compat.api_key / host），无法运行")
            log_error("[配置] 请先在 config.json 中配置 openai_compat 部分，至少需要 host / api_key / model")
            sys.exit(1)

    stats = {"found": 0, "new": 0, "processed": 0, "posted": 0, "post_failed": 0,
             "bili_posted": 0, "bili_failed": 0, "bili_review": 0}
    exit_code = 0

    try:
        if args.post_only:
            if args.publish in ("all", "mcbbs"):
                result = run_post_only(config, config_path=args.config)
                stats["posted"] = result["success"]
                stats["post_failed"] = result["failed"]
            if args.publish in ("all", "bilibili"):
                bili_result = run_post_only_bili(config, backfill=args.bili_backfill)
                stats["bili_posted"] = bili_result["success"]
                stats["bili_failed"] = bili_result["failed"]
                stats["bili_review"] = bili_result["pending_review"]
            if args.publish == "none":
                log_info("[主] --publish none：不做任何发布")
        elif args.dry_run:
            log_info("[模式] Dry Run — 仅检测新新闻，不翻译、不保存、不发布")
            run_scrape(config, state_file, dry_run=True, stats=stats)
        else:
            # 全流程 或 scrape-only
            processed = run_scrape(config, state_file, stats=stats)
            stats["processed"] = len(processed)
            publish_target = "none" if args.scrape_only else args.publish
            # 没抓到新文章时也要跑发布：登录失败留下的文章、以及审核过后才能补的
            # 帖子高亮都在等下一轮，不靠新文章触发
            has_mcbbs_work = (_has_unposted_mcbbs(config)
                              or _has_pending_mcbbs_highlights(config))
            can_backfill = publish_target in ("all", "mcbbs") and has_mcbbs_work

            if publish_target == "none":
                if processed:
                    log_info("[主] 跳过发布（%d 篇已生成产物）", len(processed))
                else:
                    log_info("[主] 没有新内容需要处理")
            elif not processed and not can_backfill:
                log_info("[主] 没有新内容需要处理")
            else:
                if not processed:
                    log_info("[主] 没有新内容，检查是否有未发布的文章或待补高亮需要处理")
                _publish_all(processed, config, args, publish_target, stats)

    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - 兜底，任何异常都要留在日志里
        log_error("[主] 运行中断: %s", e, exc_info=True)
        exit_code = 1
    finally:
        log_info(
            "[汇总] 模式=%s | 来源 %d 条 | 新增 %d 条 | 翻译完成 %d 篇 | "
            "MCBBS 成功 %d/失败 %d | 专栏 成功 %d（待审核 %d）/失败 %d | 总用时 %s",
            mode, stats["found"], stats["new"], stats["processed"],
            stats["posted"], stats["post_failed"],
            stats["bili_posted"], stats["bili_review"], stats["bili_failed"],
            elapsed_since(run_started),
        )
        log_section("MCTTK 运行结束")

    if exit_code:
        sys.exit(exit_code)


def _publish_all(processed: list, config: dict, args, publish_target: str, stats: dict) -> None:
    """按 --publish 与各平台 enabled 开关依次发布，结果写回 stats。"""
    mcbbs_enabled = (config.get("mcbbs") or {}).get("enabled", False)
    bili_enabled = (config.get("bilibili") or {}).get("enabled", False)

    if publish_target in ("all", "mcbbs"):
        if mcbbs_enabled:
            log_section("开始发布到 MCBBS")
            result = run_post(processed, config, no_image=args.no_image, no_json=args.no_json,
                              config_path=args.config)
            stats["posted"] = result["success"]
            stats["post_failed"] = result["failed"]
        else:
            log_warning("[主] 目标是 MCBBS，但 mcbbs.enabled = false，跳过"
                        "（在 config.json 里把 mcbbs.enabled 设为 true）")

    if publish_target in ("all", "bilibili"):
        if bili_enabled:
            log_section("开始发布到 B站专栏")
            bili_result = run_post_bili(processed, config)
            stats["bili_posted"] = bili_result["success"]
            stats["bili_failed"] = bili_result["failed"]
            stats["bili_review"] = bili_result["pending_review"]
        else:
            log_warning("[主] 目标是专栏，但 bilibili.enabled = false，跳过"
                        "（在 config.json 里把 bilibili.enabled 设为 true）")


if __name__ == "__main__":
    main()
