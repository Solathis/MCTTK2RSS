#!/usr/bin/env python3
"""
main.py — MCTTK 新闻自动爬取 + 翻译 + 转换 + 发布 编排器

工作流程：
  1. 从 Minecraft 官方 API 获取最新新闻
  2. 按 news_types 配置过滤类型（Java 正式版/快照/预发布/RC、基岩版正式版/测试版）
  3. 检查已发布状态，跳过已处理的新闻
  4. 逐篇处理：解析 → 翻译 → 保存 JSON → 转换 BBCode/Markdown → 发布到 MCBBS
  5. 记录运行汇总（来源条数 / 新增 / 翻译篇数 / 发帖成功失败 / 发帖地址 / 耗时）

用法：
  python main.py                    # 自动运行全流程
  python main.py --dry-run          # 仅检测，不翻译也不发布
  python main.py --scrape-only      # 只爬取+翻译+转换，不发布
  python main.py --post-only        # 只发布 output 目录中未发布的文件

配置：
  统一使用 config/config.json
  环境变量覆盖：OPENAI_API_KEY, MCBBS_USERNAME, MCBBS_PASSWORD 等
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

from converter import convert_json_file
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
from utils import CONFIG_DIR, load_dotenv

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
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
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


def _filter_and_check_state(all_news: list, config: dict, state_file: str):
    """
    类型过滤 + 加载状态 + 首次运行检测 + 过滤已处理。

    Returns:
        (new_news, state, posted_urls) 或 None（首次运行/无新内容时）
    """
    api_items = [n for n in all_news if n.get('_source') == 'minecraft_api']
    feedback_items = [n for n in all_news if n.get('_source') == 'feedback']
    filtered_api = filter_news_by_types(api_items, config)
    filtered = filtered_api + feedback_items
    log_debug("[主] 参与状态比对的候选: %d 条（API %d + feedback %d）",
              len(filtered), len(filtered_api), len(feedback_items))

    state = load_state(state_file)
    posted_urls = set(state.get("posted_urls", []))

    if state.get("_first_run", False):
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

    result = _filter_and_check_state(all_news, config, state_file)
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

    modules_cfg_path = os.path.join(CONFIG_DIR, "modules_config.json")
    modules_cfg = None
    if os.path.exists(modules_cfg_path):
        with open(modules_cfg_path, encoding="utf-8") as f:
            modules_cfg = json.load(f)
        log_debug("[主] 已加载模块配置: %s", modules_cfg_path)
    else:
        log_debug("[主] 未找到模块配置，将使用内置默认模块: %s", modules_cfg_path)

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


def run_post(processed: list, config: dict, no_image: bool = False, no_json: bool = False) -> dict:
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

    if not processed:
        log_info("[发布] 没有需要发布的文章")
        return result

    mcbbs_config = load_poster_config()
    log_info("[发布] 目标站点: %s | 版块 fid=%s | 待发布 %d 篇",
             mcbbs_config.get("base_url"), mcbbs_config.get("forum_fid"), len(processed))

    poster = MCBBSPoster(mcbbs_config)
    login_started = time.monotonic()
    try:
        poster.login()
    except Exception as e:
        log_error("[发布] MCBBS 登录失败，本次不发布: %s", e, exc_info=True)
        result["failed"] = len(processed)
        return result
    log_info("[发布] 登录成功，用时 %s", elapsed_since(login_started))

    save_dir = config["output"]["save_dir"]
    poster_state_file = os.path.join(save_dir, ".posted.json")
    posted = load_posted(poster_state_file)

    for index, (stem, txt_path, json_path) in enumerate(processed, 1):
        post_started = time.monotonic()
        log_info("[发布] %d/%d 开始发帖: %s", index, len(processed), stem)
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


def run_post_only(config: dict) -> dict:
    """仅发布 output 目录中未发布的文件"""
    from poster import MCBBSPoster, load_posted, load_poster_config, save_posted

    result = {"success": 0, "failed": 0, "urls": []}
    started = time.monotonic()

    mcbbs_config = load_poster_config()
    if not config.get("mcbbs", {}).get("enabled", False):
        log_warning("[发布] MCBBS 发布未启用（config.json 中 mcbbs.enabled = false）")
        return result

    save_dir = config["output"]["save_dir"]

    all_news = []
    for txt_path in sorted(glob.glob(os.path.join(save_dir, "*.txt"))):
        stem = os.path.splitext(os.path.basename(txt_path))[0]
        json_path = os.path.join(save_dir, stem + ".json")
        if os.path.exists(json_path):
            all_news.append((stem, txt_path, json_path))

    if not all_news:
        log_warning("[发布] output 目录没有可发布的新闻文件（需要 .txt + 同名 .json）: %s", save_dir)
        return result

    # 使用 poster 的状态文件
    poster_state_file = os.path.join(save_dir, ".posted.json")
    posted = load_posted(poster_state_file)
    pending = [(s, t, j) for s, t, j in all_news if s not in posted]

    if not pending:
        log_info("[发布] 所有 %d 个文件已发布过，无需处理", len(all_news))
        return result

    log_info("[发布] 待发布: %d 个（共 %d 个文件，已发布 %d 个）",
             len(pending), len(all_news), len(all_news) - len(pending))

    poster = MCBBSPoster(mcbbs_config)
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
    log_info("[配置] 日志文件: %s", current_log_path() or "-")


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
    parser.add_argument("--scrape-only", action="store_true", help="只爬取+翻译+转换，不发布到 MCBBS")
    parser.add_argument("--post-only", action="store_true", help="只发布 output 目录中未发布的文件")
    parser.add_argument("--no-image", action="store_true", help="发布时跳过图片上传")
    parser.add_argument("--no-json", action="store_true", help="发布时跳过 JSON 附件上传")
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

    stats = {"found": 0, "new": 0, "processed": 0, "posted": 0, "post_failed": 0}
    exit_code = 0

    try:
        if args.post_only:
            result = run_post_only(config)
            stats["posted"] = result["success"]
            stats["post_failed"] = result["failed"]
        elif args.dry_run:
            log_info("[模式] Dry Run — 仅检测新新闻，不翻译、不保存、不发布")
            run_scrape(config, state_file, dry_run=True, stats=stats)
        else:
            # 全流程 或 scrape-only
            processed = run_scrape(config, state_file, stats=stats)
            stats["processed"] = len(processed)

            mcbbs_enabled = config.get("mcbbs", {}).get("enabled", False)
            if processed and not args.scrape_only and mcbbs_enabled:
                log_section("开始发布到 MCBBS")
                result = run_post(processed, config, no_image=args.no_image, no_json=args.no_json)
                stats["posted"] = result["success"]
                stats["post_failed"] = result["failed"]
            elif processed and args.scrape_only:
                log_info("[主] Scrape-only 模式，跳过发布（%d 篇待发布）", len(processed))
            elif processed and not mcbbs_enabled:
                log_warning("[主] 已生成 %d 篇内容，但 mcbbs.enabled = false，跳过发布", len(processed))
            elif not processed:
                log_info("[主] 没有新内容需要处理")

    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - 兜底，任何异常都要留在日志里
        log_error("[主] 运行中断: %s", e, exc_info=True)
        exit_code = 1
    finally:
        log_info(
            "[汇总] 模式=%s | 来源 %d 条 | 新增 %d 条 | 翻译完成 %d 篇 | 发帖成功 %d | 发帖失败 %d | 总用时 %s",
            mode, stats["found"], stats["new"], stats["processed"],
            stats["posted"], stats["post_failed"], elapsed_since(run_started),
        )
        log_section("MCTTK 运行结束")

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
