#!/usr/bin/env python3
"""
poster.py — MCBBS 自动发帖模块（新闻模式 + 图片上传）

支持两种使用方式：
  1. CLI 模式：python poster.py [选项]（与原 post.py 兼容）
  2. 模块导入：from poster import MCBBSPoster（供 main.py 调用）

文件命名规则：
  news_xxx.txt  → 帖子正文（MCBBS BBCode）
  news_xxx.json → 元数据，需包含 "title" 字段
  news_xxx.jpg  → 题图（可选）
"""

import argparse
import contextlib
import glob
import json
import os
import re
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote, unquote

import requests

from log_setup import current_log_path, log_debug, log_error, log_info, log_warning, setup_logging
from utils import (
    CONFIG_DIR,
    MODULE_TYPE_MAP,
    PROJECT_ROOT,
    classify_article_type,
    find_image,
    load_dotenv,
    load_posted,
    load_thread_records,
    save_posted,
    save_thread_records,
    thread_records_path,
)

# ── 配置 ─────────────────────────────────────────────

_CATEGORY_GROUP = {
    'java_snapshot': 'Java资讯',
    'java_prerelease': 'Java资讯',
    'java_rc': 'Java资讯',
    'java_release': 'Java资讯',
    'bedrock_beta': '基岩版资讯',
    'bedrock_release': '基岩版资讯',
    'commentary': '块讯',
    'normal': '块讯',
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

IMG_INSERT_BEFORE = "[align=center][size=5][b]NEWS[/b]"

# 登录重试默认值，config.json 的 retry.login 可以覆盖（见 load_poster_config）
DEFAULT_LOGIN_RETRY = {"max_attempts": 3, "wait_seconds": 10}

# 登录链路的请求必须有超时：站点或网络挂住时，超时才能把它变成可重试的失败。
# 这里是交互式登录，不用抓取链路的 http.timeout（那是有意给长下载留的余量）。
# 列表页、高亮、过审探测这几条短请求也共用这个超时。
LOGIN_TIMEOUT = 30

# 发帖响应的跳转地址有三种写法：静态化地址 thread-123-1-1.html、查询串
# forum.php?mod=viewthread&tid=123，以及弹窗脚本里被转义的 window.location.href =\'...\'。
# 旧实现只认第一种且要求引号不被转义，于是"进审核"的帖子永远拿不到帖子 ID。
_REDIRECT_JS_RE = re.compile(r"window\.location\.href\s*=\s*\\?'([^']+?)\\?'")
_TID_URL_RE = re.compile(r"thread-(\d+)-1-1")
_TID_QUERY_RE = re.compile(r"[?&](?:amp;)?tid=(\d+)")


# Discuz 的标题高亮色板（forum_colorarray）。下标就是要提交的 highlight_color，
# 色值与版规「二、高亮设定」里写的完全一致，所以配置用色值、代码翻译成色号。
_HIGHLIGHT_COLORS = {
    "#EE1B2E": 1,
    "#EE5023": 2,
    "#996600": 3,
    "#3C9D40": 4,
    "#2897C5": 5,
    "#2B65B7": 6,
    "#8F2A90": 7,
    "#EC1282": 8,
}

# 高亮默认值：色号取自幻翼块讯版规「二、高亮设定」，commentary（时评）按版规一般不高亮。
DEFAULT_HIGHLIGHT = {
    "enabled": True,
    "colors": {
        "java_release": "#EE1B2E",
        "java_snapshot": "#3C9D40",
        "java_prerelease": "#3C9D40",
        "java_rc": "#3C9D40",
        "bedrock_release": "#EE1B2E",
        "bedrock_beta": "#3C9D40",
        "normal": "#EC1282",
    },
    "bold_types": [
        "java_release",
        "java_snapshot",
        "java_prerelease",
        "java_rc",
        "bedrock_release",
        "bedrock_beta",
    ],
    "exclude_forums": ["块讯回收区"],
    "max_attempts": 6,
    "max_age_hours": 168,
    "max_age_days": 30,
}


# 版规「最新版本加粗」是按版本线分开算的，而且 Java 版和基岩版各算各的：
# 一共 4 个「最新」——Java 正式版（红）、Java 测试版（绿）、基岩版正式版（红）、
# 基岩版测试版（绿）。分类（typeid 4/20）本来就分开，所以收敛的分组键是
# (typeid, 正式版/测试版)。站点列表里的行没有 news_type，按标题认（见 _bold_family）。
_BOLD_RELEASE_TYPES = {"java_release", "bedrock_release"}
_BOLD_PREVIEW_TYPES = {"java_snapshot", "java_prerelease", "java_rc", "bedrock_beta"}
_FAMILY_NAMES = {"release": "正式版", "preview": "测试版"}


def highlight_color_index(color: str) -> int | None:
    """把配置里的色值翻成 Discuz 的高亮色号；不在色板内返回 None。"""
    return _HIGHLIGHT_COLORS.get((color or "").strip().upper())


def _merge_highlight(raw) -> dict:
    """合并 mcbbs.highlight：colors 逐项覆盖，其余键只认合法类型，非法值保持默认。"""
    merged = deepcopy(DEFAULT_HIGHLIGHT)
    if not isinstance(raw, dict):
        return merged
    if isinstance(raw.get("enabled"), bool):
        merged["enabled"] = raw["enabled"]
    colors = raw.get("colors")
    if isinstance(colors, dict):
        merged["colors"].update({str(k): v for k, v in colors.items() if isinstance(v, str)})
    bold_types = raw.get("bold_types")
    if isinstance(bold_types, list):
        merged["bold_types"] = [str(item) for item in bold_types]
    exclude_forums = raw.get("exclude_forums")
    if isinstance(exclude_forums, list):
        merged["exclude_forums"] = [
            item for item in exclude_forums
            if not isinstance(item, bool) and isinstance(item, (int, str))
        ]
    for key in ("max_attempts", "max_age_hours", "max_age_days"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class PostResult:
    """一次发帖的结果。

    帖子 ID 与「要不要审核」必须一起带出来：Discuz 对 displayorder < 0（审核中）
    的帖子拒绝设置高亮，而这类响应往往又不带可用的帖子地址。少了这两个值，
    审核中的帖子就再也补不上高亮了。
    """

    url: str
    tid: int | None = None
    needs_audit: bool = False


def _extract_tid(text: str) -> int | None:
    """从地址或页面片段里取帖子 ID。"""
    match = _TID_URL_RE.search(text or "") or _TID_QUERY_RE.search(text or "")
    return int(match.group(1)) if match else None


def _find_redirect(html: str) -> str:
    """取发帖响应里脚本跳转的目标地址；优先带帖子 ID 的那一条。"""
    targets = [match.group(1) for match in _REDIRECT_JS_RE.finditer(html or "")]
    for target in targets:
        if _extract_tid(target):
            return target
    return targets[0] if targets else ""


# 记录里的时间戳格式（ISO 风格、秒级、本地时间），发帖时间/登记时间/更新时间共用
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _timestamp() -> str:
    """记录用的本地时间戳（秒级，ISO 风格，便于人眼与排序）。"""
    return time.strftime(TIMESTAMP_FORMAT)


def _parse_timestamp(text: str) -> float | None:
    """解析记录里的时间戳，解析不了返回 None。"""
    try:
        return time.mktime(time.strptime(text or "", TIMESTAMP_FORMAT))
    except (TypeError, ValueError):
        return None


def _record_time(record: dict) -> float | None:
    """记录的「起点」时间：优先发帖时间，其次登记时间。

    刻意不看 `updated_at` —— 它每轮都被刷新，拿它算年龄等于永不过期（这正是审核中的
    帖子被无限重试的原因）。两个字段都没有的老记录由 `_prune_records` 补一次
    `registered_at`，从那一次开始算年龄。
    """
    return (_parse_timestamp(record.get("posted_at") or "")
            or _parse_timestamp(record.get("registered_at") or ""))


def _records_digest(records: dict) -> str:
    """记录内容的指纹：这一轮没改动就不重写文件（每小时一轮，别每次都动磁盘）。"""
    return json.dumps(records, ensure_ascii=False, sort_keys=True)


def _waiting_since(record: dict) -> float | None:
    """「等审核」的起算点：登记（或被重新提升）的时间，不是发帖时间。

    这两个时间必须分开：历史帖的发帖时间可能是几个月前，`max_age_days` 用它判断
    「值不值得管」，而 `max_age_hours` 判断的是「这次等待已经持续多久」，只能从登记算，
    否则把一篇老帖提升上来就等于立刻超时。
    """
    return (_parse_timestamp(record.get("registered_at") or "")
            or _parse_timestamp(record.get("posted_at") or ""))


_MY_THREAD_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_TID_IN_ANCHOR_RE = re.compile(
    # 标题链接的页码部分会跟着列表页走（第 2 页是 thread-6720-1-2.html），
    # 早期只认 -1-1 时整页都当"没有标题"，版块列表只能读到第 1 页。
    r'href="(?:forum\.php\?mod=viewthread&(?:amp;)?tid=(\d+)|thread-(\d+)-\d+-\d+\.html)'
)
_FORUM_LINK_RE = re.compile(
    r'forum\.php\?mod=forumdisplay&(?:amp;)?fid=(\d+)[^"]*"[^>]*>([^<]{1,40})</a>', re.I
)
_STRUCK_INLINE_RE = re.compile(r"line-through", re.I)
_STRUCK_TAG_RE = re.compile(r"^\s*<(del|s|strike)\b", re.I)


@dataclass(frozen=True)
class MyThread:
    """「我的帖子」列表里的一行。

    recy 是站点对回收站帖子的标记（标题加删除线，Discuz 模板的 class="recy"）：
    这类帖子要么已删除、要么测试后删除、要么人工翻译后撤掉 AI 翻译版，
    都不该再回填高亮。forum_* 用来按版块排除（例如块讯回收区）。
    """

    tid: int
    title: str
    recy: bool = False
    forum_fid: int | None = None
    forum_name: str = ""


@dataclass(frozen=True)
class ThreadProbe:
    """帖子页探测结果：游客可见性，以及面包屑里透露的版块信息。"""

    visible: bool | None
    forum_fid: int | None = None
    crumb: str = ""


_FORUM_ROW_RE = re.compile(r'<tbody id="normalthread_(\d+)"', re.I)
_FORUM_TYPE_RE = re.compile(r"typeid=(\d+)[^>]*>([^<]{1,20})</a>")
# 发帖日期在**第一个** class="by" 单元格里；紧跟的第二个 by 单元格是「最后回复」，
# 跨过去就会把被顶起来的老帖当成新帖。近期写成 title="2026-11-18"，早一些的直接是
# <span>2026-11-4</span>（月和日不补零）。
_POSTED_CELL_RE = re.compile(r'<td class="by">(?:(?!</td>).)*', re.S | re.I)
_POSTED_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_STYLE_ATTR_RE = re.compile(r'style="([^"]*)"', re.I)
_COLOR_HEX_RE = re.compile(r"color:\s*(#[0-9A-Fa-f]{6})")
_BOLD_CSS_RE = re.compile(r"font-weight:\s*bold", re.I)


@dataclass(frozen=True)
class ForumThread:
    """版块列表里的一行：tid、标题、分类，以及它当前的高亮状态。

    版块列表是唯一能看到「颜色 + 是否加粗」的地方（标题链接上的内联 style），
    「我的帖子」页只有标题，帖子页本身不渲染高亮样式。posted 是列表上显示的发帖
    日期（YYYY-MM-DD），用来判断帖子本身是否已经过期——注意列表默认按最后回复排序，
    行序不代表发帖时间。
    """

    tid: int
    title: str
    typeid: int | None = None
    type_name: str = ""
    color: str = ""
    bold: bool = False
    posted: str = ""


def _posted_cell(block: str) -> str:
    """取版块列表一行里第一个 class="by" 单元格的内容（发帖时间那一格）。

    第二个 by 单元格是「最后回复」，被顶起来的老帖在那里是最新日期；只截到 `</td>`
    才不会把回复时间当成发帖时间。
    """
    match = _POSTED_CELL_RE.search(block or "")
    return match.group(0) if match else ""


def _parse_forum_threads(html: str) -> list[ForumThread]:
    """解析版块列表：按 normalthread_<tid> 分块，取标题、分类与内联高亮样式。

    一个块里有好几个指向同一帖子的链接（文件夹图标、标题、回复数），图标那个没有
    文字也没有样式，所以这里挑第一个「有实际标题文字」的锚点，高亮样式只认它。
    """
    page = html or ""
    rows = list(_FORUM_ROW_RE.finditer(page))
    threads: list[ForumThread] = []
    for index, row in enumerate(rows):
        end = rows[index + 1].start() if index + 1 < len(rows) else len(page)
        block = page[row.end():end]

        title = ""
        style = ""
        for anchor in _MY_THREAD_ANCHOR_RE.finditer(block):
            attrs, inner = anchor.group(1), anchor.group(2)
            if not _TID_IN_ANCHOR_RE.search(attrs):
                continue
            text = _normalize_title(inner)
            if not text or text.isdigit():
                continue
            title = text
            style_match = _STYLE_ATTR_RE.search(attrs)
            style = style_match.group(1) if style_match else ""
            break
        if not title:
            continue

        color_match = _COLOR_HEX_RE.search(style)
        type_match = _FORUM_TYPE_RE.search(block)
        posted_match = _POSTED_DATE_RE.search(_posted_cell(block))
        threads.append(ForumThread(
            tid=int(row.group(1)),
            title=title,
            typeid=int(type_match.group(1)) if type_match else None,
            type_name=_normalize_title(type_match.group(2)) if type_match else "",
            color=color_match.group(1).upper() if color_match else "",
            bold=bool(_BOLD_CSS_RE.search(style)),
            posted=(f"{int(posted_match.group(1)):04d}-{int(posted_match.group(2)):02d}-"
                    f"{int(posted_match.group(3)):02d}") if posted_match else "",
        ))
    return threads


def _normalize_title(text: str) -> str:
    """标题归一：去标签、反转义实体、压空白，用于和记录里的标题比对。"""
    plain = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", unescape(plain)).strip()


def _is_struck(attrs: str, inner: str) -> bool:
    """标题是否带删除线：Discuz 用 class="recy"，个别模板写成内联样式或 <del>。"""
    if _STRUCK_INLINE_RE.search(attrs or ""):
        return True
    lowered = (attrs or "").lower()
    if re.search(r'class="[^"]*\brecy\b', lowered):
        return True
    return bool(_STRUCK_TAG_RE.match(inner or ""))


def _parse_my_threads(html: str) -> list[MyThread]:
    """从「我的帖子」列表里取每行帖子的 tid、标题、删除线和版块。

    列表页每行都有多个指向同一帖子的链接（标题、回复数、最后回复），所以按 tid
    去重、只留第一次出现的那个。版块名取该行标题之后那一格里的版块链接（Discuz
    默认模板版块列在标题右侧）；取不到就留空，补高亮阶段还会按帖子页面包屑复核。
    """
    page = html or ""
    anchors: list[tuple[int, int, int, str, str]] = []
    seen: set[int] = set()
    for match in _MY_THREAD_ANCHOR_RE.finditer(page):
        attrs, inner = match.group(1), match.group(2)
        tid_match = _TID_IN_ANCHOR_RE.search(attrs)
        tid = int(tid_match.group(1) or tid_match.group(2)) if tid_match else None
        title = _normalize_title(inner)
        if not tid or not title or title.isdigit() or tid in seen:
            continue
        seen.add(tid)
        anchors.append((match.start(), match.end(), tid, attrs, inner))

    threads: list[MyThread] = []
    for index, (_start, end, tid, attrs, inner) in enumerate(anchors):
        next_start = anchors[index + 1][0] if index + 1 < len(anchors) else len(page)
        forum_match = _FORUM_LINK_RE.search(page[end:next_start])
        threads.append(MyThread(
            tid=tid,
            title=_normalize_title(inner),
            recy=_is_struck(attrs, inner),
            forum_fid=int(forum_match.group(1)) if forum_match else None,
            forum_name=_normalize_title(forum_match.group(2)) if forum_match else "",
        ))
    return threads


def _forum_excluded(forum_name: str, forum_fid: int | None, exclude) -> str:
    """命中版块排除名单时返回原因，否则空串。

    名单里字符串按"版块名包含"匹配（站点的回收区名字未必只有一种写法），
    整数按 fid 精确匹配。
    """
    for item in exclude or []:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            if forum_fid is not None and forum_fid == item:
                return f"版块 fid={item} 已排除"
            continue
        name = str(item).strip()
        if name and name in (forum_name or ""):
            return f"版块「{name}」已排除"
    return ""


_PROBE_CRUMB_RE = re.compile(r'<div id="pt"[^>]*>(.*?)</div>', re.S)
_PROBE_FID_RE = re.compile(r"forum\.php\?mod=forumdisplay&(?:amp;)?fid=(\d+)")

_PREVIEW_MARKERS = ("快照", "测试", "预览", "候选", "预发布",
                    # "prerelease" 要单独列：它含 "release"，漏了就落进正式版那一组
                    "snapshot", "preview", "beta", "candidate", "prerelease",
                    "pre-release", "pre release")
# 正式版的标题里没有统一词：Java 用「Java版 xx」，基岩版用「基岩版 xx 更新日志/热修复」。
_RELEASE_MARKERS = ("java版", "正式版", "热修复", "更新日志", "release")


def _bold_family(title: str, news_type: str = "") -> str:
    """版本帖属于「正式版」还是「测试版」，认不出返回空串。

    加粗是按这两条版本线分开收敛的（见 _unbold_older_versions / reconcile_bold），
    所以标题和 news_type 两种来源都要能判。站点列表里的行没有 news_type，只能看标题：
    先认测试版关键词（"Release Candidate" 里也有 release，必须先判它），再认正式版，
    最后交给 classify_article_type 兜底（它认得只有版本号的那种老标题）。
    """
    if news_type in _BOLD_RELEASE_TYPES:
        return "release"
    if news_type in _BOLD_PREVIEW_TYPES:
        return "preview"
    text = (title or "").lower()
    if any(marker in text for marker in _PREVIEW_MARKERS):
        return "preview"
    if any(marker in text for marker in _RELEASE_MARKERS):
        return "release"
    kind = classify_article_type(text, chinese=True, fallback="")
    if kind in _BOLD_RELEASE_TYPES:
        return "release"
    if kind in _BOLD_PREVIEW_TYPES:
        return "preview"
    return ""


def probe_thread(base_url: str, tid: int, timeout: int = LOGIN_TIMEOUT) -> ThreadProbe:
    """以游客身份探测帖子：能不能被看到，以及它落在哪个版块。

    必须用不带 cookie 的会话：作者本人和版主都能看到审核中的帖子，拿登录态探测
    会把「审核中」误判成「已过审」。Discuz 对审核中或已删除的帖子回 404 提示页。
    """
    url = f"{base_url}/thread-{tid}-1-1.html"
    try:
        with requests.Session() as session:
            session.headers.update({"User-Agent": UA})
            response = session.get(url, timeout=timeout)
    except requests.RequestException:
        log_debug("    帖子探测失败: %s", url, exc_info=True)
        return ThreadProbe(None)
    if response.status_code == 404:
        return ThreadProbe(False)
    if response.status_code >= 400:
        return ThreadProbe(None)

    html = response.text or ""
    if 'id="thread_subject"' not in html:
        return ThreadProbe(False)
    block = _PROBE_CRUMB_RE.search(html)
    crumb_html = block.group(1) if block else ""
    # 面包屑可能有多级版块（父版块在前）：排除规则针对帖子真正所在的那一级，
    # 所以取最后一个 forumdisplay 链接，而不是第一个
    fids = _PROBE_FID_RE.findall(crumb_html)
    return ThreadProbe(
        True,
        forum_fid=int(fids[-1]) if fids else None,
        crumb=_normalize_title(crumb_html),
    )



def _dump_threads_html(html: str) -> str:
    """把「我的帖子」页面落到日志目录，解析失败时用来对站点的自定义模板。"""
    path = os.path.join(
        _login_debug_dir(), f"mcbbs_my-threads_{time.strftime('%Y%m%d-%H%M%S')}.html"
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_scrub_login_html(html or ""))
    except OSError:
        log_debug("    「我的帖子」页面落盘失败", exc_info=True)
        return ""
    return path


_MODERATION_ERROR_RE = re.compile(r"errorhandle_\w+\(\s*'((?:[^'\\]|\\.)*)'")


def _parse_moderation_result(html: str) -> tuple[bool, str]:
    """解析 forum.php?mod=topicadmin 的 ajax 返回，给出 (是否成功, 失败原因)。

    Discuz 只在 inajax=1 且请求带 redirect 时才把成功写成 succeedhandle_mods(...)，
    失败一律走 errorhandle_mods(站点提示语)。两者都不出现，说明站点改版或会话失效；
    旧实现只认 succeedhandle，所以即使高亮成功也会被判成失败。
    """
    text = html or ""
    if "succeedhandle" in text:
        return True, ""
    match = _MODERATION_ERROR_RE.search(text)
    if match:
        return False, match.group(1).replace("\\'", "'")[:200]
    message = re.search(r'id="messagetext"[^>]*>(.*?)</div>', text, re.S)
    if message:
        return False, re.sub(r"<[^>]+>", "", message.group(1)).strip()[:200]
    return False, f"未识别的管理操作响应（{len(text)} 字节）"


# ── 配置加载 ─────────────────────────────────────────

def load_poster_config(config_path: str = None) -> dict:
    """从统一 config.json 中提取 mcbbs 相关配置"""
    if config_path is None:
        config_path = os.path.join(CONFIG_DIR, "config.json")

    cfg = {
        "base_url": "https://www.mcbbs.co",
        "forum_fid": 2,
        "username": "",
        "password": "",
        "captcha_answer": "",
        "sortid_map": {},
        "login_retry": dict(DEFAULT_LOGIN_RETRY),
        "highlight": deepcopy(DEFAULT_HIGHLIGHT),
    }

    if os.path.exists(config_path):
        try:
            # utf-8-sig：与 scraper.load_config 同口径。带 BOM 的配置在这里读失败
            # 会静默回落默认值（账号、版块、色板全丢），发帖阶段才发现就太晚了
            with open(config_path, encoding="utf-8-sig") as f:
                full_config = json.load(f)
            mcbbs = full_config.get("mcbbs", {})
            for key in cfg:
                if key in mcbbs and mcbbs[key]:
                    cfg[key] = mcbbs[key]
            # highlight 是带默认值的嵌套段，整体覆盖会把默认色板冲掉，单独合并
            cfg["highlight"] = _merge_highlight(mcbbs.get("highlight"))
            # 登录重试参数在顶层 retry 段（与 retry.translation / retry.download 并列）
            login_retry = (full_config.get("retry") or {}).get("login") or {}
            for key in DEFAULT_LOGIN_RETRY:
                value = login_retry.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    cfg["login_retry"][key] = value
        except Exception as e:
            log_warning(f"[配置] 读取 mcbbs 配置失败: {e}")

    # 环境变量覆盖
    env_map = {
        "MCBBS_BASE_URL": "base_url",
        "MCBBS_FORUM_FID": "forum_fid",
        "MCBBS_USERNAME": "username",
        "MCBBS_PASSWORD": "password",
        "MCBBS_CAPTCHA_ANSWER": "captcha_answer",
    }
    for env_name, cfg_key in env_map.items():
        val = os.environ.get(env_name)
        if val:
            if cfg_key == "forum_fid":
                with contextlib.suppress(ValueError):
                    val = int(val)
            cfg[cfg_key] = val

    return cfg


# ── 辅助函数 ─────────────────────────────────────────

def detect_module_type(message: str, title: str) -> str | None:
    for tag, category in MODULE_TYPE_MAP.items():
        if f"[{tag}]" in message:
            return category
    from utils import classify_article_type
    return classify_article_type(title, chinese=True, fallback=None)


def _resolve_news_type(message: str, original_title: str, translated_title: str) -> str:
    """分类优先级：BBCode 里的 [module_*] 标签 → 英文标题关键词 → 中文标题关键词 → normal。

    同时被「发帖」和「历史登记」两条路径使用：同一篇帖子在两处的分类必须一致，
    否则登记的颜色和下次重算的颜色会打架。
    """
    for title in (original_title, translated_title):
        module_type = detect_module_type(message, title) if title else None
        if module_type:
            return module_type
    return "normal"


def _scan_output_titles(save_dir: str) -> dict[str, str]:
    """扫 output 里的新闻 JSON，返回 {归一后标题: stem}。

    帖子标题是「译文标题 + [AI翻译]」，早期帖子可能没有后缀，所以两种写法都收。
    专栏正文产物（*.opus.json）不是新闻元数据，跳过。
    """
    titles: dict[str, str] = {}
    for json_path in sorted(glob.glob(os.path.join(save_dir, "*.json"))):
        stem = os.path.splitext(os.path.basename(json_path))[0]
        if stem.startswith(".") or stem.endswith(".opus"):
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("translated_title", "title"):
            value = (meta.get(key) or "").strip()
            if not value:
                continue
            for candidate in (f"{value} [AI翻译]", value):
                titles.setdefault(_normalize_title(candidate), stem)
    return titles


def _local_news_type(save_dir: str, stem: str) -> str:
    """按产物反推一篇新闻的分类（BBCode 在就用标签，否则只看标题）。"""
    json_path = os.path.join(save_dir, stem + ".json")
    try:
        with open(json_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "normal"
    txt_path = os.path.join(save_dir, stem + ".txt")
    message = ""
    if os.path.isfile(txt_path):
        with open(txt_path, encoding="utf-8") as f:
            message = f.read()
    return _resolve_news_type(
        message,
        (meta.get("title") or "").strip(),
        (meta.get("translated_title") or "").strip(),
    )


_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_STEM_MONTH_RE = re.compile(
    r"(\d{1,2})_(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)_(\d{4})")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _artifact_release_time(save_dir: str, stem: str) -> float | None:
    """产物的发布时间：先读 JSON 的 release_date，其次从文件名里的日期推。

    只给「多久以前的事就不用管了」这条判断用，时区差几个小时无所谓。两者都没有就
    返回 None（例如早期产物），调用方按「无法判断」处理，不因为算不出年龄就误杀。
    """
    json_path = os.path.join(save_dir, stem + ".json")
    try:
        with open(json_path, encoding="utf-8") as f:
            release = (json.load(f).get("release_date") or "").strip()
    except (OSError, json.JSONDecodeError):
        release = ""
    iso = _ISO_DATE_RE.search(release) or _ISO_DATE_RE.search(stem)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
        with contextlib.suppress(ValueError, OverflowError):
            return time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
    named = _STEM_MONTH_RE.search(stem)
    if named:
        day, month_name, year = int(named.group(1)), named.group(2), int(named.group(3))
        with contextlib.suppress(ValueError, OverflowError):
            return time.mktime((year, _MONTHS.index(month_name) + 1, day, 0, 0, 0, 0, 0, -1))
    return None


def insert_image_bbcode(message: str, aid: str) -> str:
    img_tag = f"[align=center][attachimg]{aid}[/attachimg][/align]"
    idx = message.find(IMG_INSERT_BEFORE)
    if idx != -1:
        return message[:idx] + img_tag + "\n\n" + message[idx:]
    hr_idx = message.find("[hr]")
    if hr_idx != -1:
        insert_pos = hr_idx + len("[hr]")
        while insert_pos < len(message) and message[insert_pos] in "\n\r ":
            insert_pos += 1
        return message[:hr_idx + len("[hr]")] + "\n\n" + img_tag + "\n\n" + message[insert_pos:]
    return img_tag + "\n\n" + message


# ── MCBBS 会话与登录 ─────────────────────────────────

# 验证码 idhash 在页面上的写法：标准模板是 updateseccode('hash', 'modid')，
# 部分模板只留 <input name="seccodehash">，或把 idhash 拼在验证码图片地址上。
_SECCODE_HASH_PATTERNS = (
    r"updateseccode\('([A-Za-z0-9]+)'",
    r'name="seccodehash"\s+value="([A-Za-z0-9]+)"',
    r"idhash=([A-Za-z0-9]+)",
)

# 记日志/落盘时关注的页面标记，用来区分登录表单、验证码表单和风控页
_LOGIN_MARKERS = ("formhash", "loginhash", "updateseccode", "seccodehash", "验证码", "messagetext")

# 站点明确拒绝凭证的提示：重试只会加重失败计数，遇到就立即停手。
# 注意不能把「验证码错误」放进来——那是 OCR 没认对，应该换一张继续试。
_PERMANENT_LOGIN_HINTS = (
    "密码错误", "用户名或密码", "用户名不存在", "用户不存在",
    "安全问题", "账号", "冻结", "禁止", "锁定", "尝试",
)


class LoginRetryable(RuntimeError):
    """登录流程里可以换新会话重试的失败。

    验证码页结构不符、风控页、会话没带上登录态都属于这一类；凭证被站点明确
    拒绝（密码错误/安全问题）不算——重试只会让失败计数继续涨，还可能触发验证码。
    """


class LoginRejected(RuntimeError):
    """站点明确拒绝凭证或账号状态（密码错误/安全问题/被限制），重试没有意义。"""


def _is_permanent_login_error(message: str) -> bool:
    """提示语是不是「重试也没用」的那一类（凭证被拒、账号被限制）。"""
    return any(hint in message for hint in _PERMANENT_LOGIN_HINTS)


def extract_formhash(html: str) -> str:
    m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
    if not m:
        raise ValueError("无法提取 formhash")
    return m.group(1)


def extract_loginhash(html: str) -> str:
    m = re.search(r'loginhash=([A-Za-z0-9]+)', html)
    return m.group(1) if m else ""


def _captcha_page_hash(html: str) -> str:
    """从带验证码的登录页里取 idhash；页面不像登录表单时返回空串。

    只认 seccode 标记不够：风控页、错误页偶尔也带 idhash，但没有 formhash 提交不了。
    """
    if 'name="formhash"' not in html:
        return ""
    for pattern in _SECCODE_HASH_PATTERNS:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return ""


def _login_debug_dir() -> str:
    """登录排查页面的落盘目录：跟日志同目录，测试里可用 MCTTK_LOG_DIR 指定。"""
    log_path = current_log_path()
    if log_path:
        return os.path.dirname(log_path)
    return os.environ.get("MCTTK_LOG_DIR") or "logs"


def _scrub_login_html(html: str) -> str:
    """落盘前抹掉登录令牌和密码框内容：日志里不许出现凭据。

    auth 的值按「到分隔符为止」整体抹掉，不假设它只含 base64 字符——站点可能用
    URL-safe 的 `-`/`_` 或带 `=` 填充，漏掉就会把会话令牌留在排查文件里。
    """
    html = re.sub(r"(auth=)[^\s\"'&<>]+", r"\1***", html)
    return re.sub(r'(name="password"[^>]*value=")[^"]*(")', r"\1***\2", html)


def _dump_login_html(tag: str, html: str) -> str:
    """把登录响应写到日志目录，返回路径；写不了返回空串。"""
    directory = _login_debug_dir()
    path = os.path.join(directory, f"mcbbs_login_{tag}_{time.strftime('%Y%m%d-%H%M%S')}.html")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_scrub_login_html(html))
    except OSError:
        log_debug("    登录页面落盘失败", exc_info=True)
        return ""
    return path


def _log_login_evidence(tag: str, label: str, response) -> str:
    """记录一次登录响应的关键特征并把页面落盘，用于定位验证码页/风控页。"""
    html = getattr(response, "text", "") or ""
    status = getattr(response, "status_code", 0)
    marks = " ".join(f"{marker}={html.count(marker)}" for marker in _LOGIN_MARKERS)
    path = _dump_login_html(tag, html)
    log_warning("[登录排查] %s: HTTP %s，%d 字节，%s；页面已存: %s",
                label, status, len(html), marks, path or "（落盘失败）")
    return path


def _verify_login(session: requests.Session, base_url: str) -> str:
    r = session.get(f"{base_url}/forum.php", timeout=LOGIN_TIMEOUT)
    r.raise_for_status()
    uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
    if not uid or uid.group(1) == "0":
        raise LoginRetryable("登录验证失败：discuz_uid=0")
    return extract_formhash(r.text)


def _init_ocr():
    """初始化 ddddocr，失败返回 None"""
    try:
        import ddddocr
        return ddddocr.DdddOcr(show_ad=False)
    except Exception:  # noqa: BLE001
        log_debug("ddddocr 初始化失败，将跳过 OCR 识别", exc_info=True)
        return None


def _preprocess_captcha(img_bytes: bytes) -> list:
    """对验证码图片做多种预处理，返回多个候选图片 bytes"""
    candidates = [img_bytes]
    try:
        import io as _io

        from PIL import Image, ImageFilter
        img = Image.open(_io.BytesIO(img_bytes))
        gray = img.convert("L")
        bw = gray.point(lambda x: 255 if x > 128 else 0, "1")
        buf = _io.BytesIO()
        bw.save(buf, format="PNG")
        candidates.append(buf.getvalue())
        big = img.convert("L").resize((img.width * 2, img.height * 2), Image.LANCZOS)
        sharp = big.filter(ImageFilter.SHARPEN)
        buf2 = _io.BytesIO()
        sharp.save(buf2, format="PNG")
        candidates.append(buf2.getvalue())
    except Exception:  # noqa: BLE001
        log_debug("验证码图片预处理失败，使用原始图片", exc_info=True)
    return candidates


def _ocr_recognize(ocr, img_bytes: bytes) -> str:
    """用 OCR 识别验证码，返回识别结果（失败返回空字符串）"""
    if not ocr:
        return ""
    candidates = _preprocess_captcha(img_bytes)
    for idx, img_data in enumerate(candidates):
        try:
            raw = re.sub(r'[^a-zA-Z0-9]', '', ocr.classification(img_data).strip())
            if 3 <= len(raw) <= 8:
                tag = "原始" if idx == 0 else f"预处理#{idx}"
                log_debug(f"    OCR ({tag}): {raw}")
                return raw
        except Exception:  # noqa: BLE001
            log_debug("OCR 识别单张图片失败", exc_info=True)
    return ""


def _submit_captcha_login(session, base_url, r_cap, idhash, auth_token,
                          username, password, answer) -> bool:
    """提交一次验证码登录，成功返回 True，失败返回 False"""
    try:
        fh_cap = extract_formhash(r_cap.text)
    except ValueError as e:
        _log_login_evidence("captcha", "验证码页", r_cap)
        raise LoginRetryable(f"验证码页缺少 formhash（{e}）") from None
    lh_cap = extract_loginhash(r_cap.text)
    seccode_field = "seccodeverify"
    if 'name="seccode"' in r_cap.text and 'name="seccodeverify"' not in r_cap.text:
        seccode_field = "seccode"
    all_modids = re.findall(r"updateseccode\('[^']+',[^,]+,\s*'([^']+)'\)", r_cap.text)
    seccodemodid = all_modids[-1] if all_modids else "member::logging"

    post_data = {
        "formhash": fh_cap,
        "referer": f"{base_url}/",
        "auth": auth_token,
        "username": username,
        "password": password,
        "questionid": "0",
        "answer": "",
        "seccodehash": idhash,
        "seccodemodid": seccodemodid,
        seccode_field: answer,
    }
    referer_url = (
        f"{base_url}/member.php?mod=logging&action=login"
        f"&auth={quote(auth_token, safe='')}&referer={base_url}/&cookietime=1"
    )
    log_debug(f"    提交: {seccode_field}={answer}, password={'(set)' if password else '(EMPTY!)'}")
    session.headers.update({"Referer": referer_url})
    r_post = session.post(
        f"{base_url}/member.php?mod=logging&action=login"
        f"&loginsubmit=yes&loginhash={lh_cap}&inajax=1",
        data=post_data,
        timeout=LOGIN_TIMEOUT,
    )
    session.headers.pop("Referer", None)

    if "欢迎您回来" in r_post.text or "succeedhandle_" in r_post.text:
        return True

    err_match = re.search(r'id="messagetext"[^>]*>(.*?)</div>', r_post.text, re.S)
    if err_match:
        err_msg = re.sub(r'<[^>]+>', '', err_match.group(1)).strip()[:200]
        if "安全问题" not in err_msg:
            log_warning(f"    ✗ 登录被拒: {err_msg}")
        # 凭证被拒和「验证码不对」必须分开：前者再试只会加重失败计数
        if _is_permanent_login_error(err_msg):
            raise LoginRejected(f"登录失败: {err_msg}")
    else:
        log_warning(f"    ✗ 未知登录结果（HTTP {r_post.status_code}，长度 {len(r_post.text)}）")
    return False


def _fetch_captcha_page(session, base_url, auth_token) -> tuple:
    """重新取一次登录页，返回 (响应, idhash, auth_token)；idhash 可能为空。"""
    r_cap = session.get(
        f"{base_url}/member.php?mod=logging&action=login"
        f"&auth={quote(auth_token, safe='')}&referer={base_url}/&cookietime=1",
        timeout=LOGIN_TIMEOUT,
    )
    auth_m = re.search(r"auth=([A-Za-z0-9%/+]+)", r_cap.text)
    if auth_m:
        auth_token = unquote(auth_m.group(1))
    return r_cap, _captcha_page_hash(r_cap.text), auth_token


def _refresh_captcha_page(session, base_url, auth_token, attempts: int = 3) -> tuple:
    """换一张验证码页面：重取登录页并确认里面带 seccode 标记。"""
    r_cap = None
    for attempt in range(1, attempts + 1):
        time.sleep(1)
        r_cap, idhash, auth_token = _fetch_captcha_page(session, base_url, auth_token)
        if idhash:
            return r_cap, idhash, auth_token
        log_debug("    重新获取的登录页里没有 seccode 标记（第 %d/%d 次）", attempt, attempts)
    _log_login_evidence("captcha-page", "重新获取的登录页", r_cap)
    raise LoginRetryable("无法提取验证码 hash（重新获取的登录页里没有 seccode 标记）")


def _select_captcha_page(session, base_url, r, auth_token, attempts: int = 3) -> tuple:
    """定位带验证码表单的页面，返回 (响应, idhash, auth_token)。

    站点要求验证码时，刚才那次失败的 POST 响应本身就是带验证码的登录页，
    优先用它；页面里没有 seccode 标记时才重新取登录页。
    """
    idhash = _captcha_page_hash(r.text)
    if idhash:
        log_debug("    验证码 hash 取自失败的登录响应（无需重取登录页）")
        return r, idhash, auth_token
    log_debug("    失败响应里没有 seccode 标记，改为重新获取登录页")
    try:
        return _refresh_captcha_page(session, base_url, auth_token, attempts)
    except LoginRetryable:
        _log_login_evidence("captcha", "失败响应", r)
        raise


def _login_with_captcha(session, base_url, username, password, captcha_answer, r):
    """处理「需要验证码」的登录，返回登录后的 formhash。

    站点是概率性要求验证码的，所以这里不轻易放弃：取不到验证码页先把响应落盘
    留证据，再抛可重试错误交给外层换会话重试。
    """
    log_warning("    ⚠ 需要验证码")
    auth_m = re.search(r"auth=([A-Za-z0-9%/+]+)", r.text)
    if not auth_m:
        _log_login_evidence("captcha", "失败响应（没有 auth token）", r)
        raise LoginRetryable("无法提取 auth token")
    auth_token = unquote(auth_m.group(1))

    r_cap, idhash, auth_token = _select_captcha_page(session, base_url, r, auth_token)
    ocr = _init_ocr()

    for attempt in range(1, 6):
        referer_url = (
            f"{base_url}/member.php?mod=logging&action=login"
            f"&auth={auth_token}&referer={base_url}/&cookietime=1"
        )
        session.headers.update({"Referer": referer_url})
        update_seed = str(int(time.time() * 1000))[-6:]
        r_img = session.get(f"{base_url}/misc.php?mod=seccode&update={update_seed}&idhash={idhash}",
                            timeout=LOGIN_TIMEOUT)
        session.headers.pop("Referer", None)

        if len(r_img.content) < 100:
            log_warning("    验证码图片为空（第%d次），换一张", attempt)
            r_cap, idhash, auth_token = _refresh_captcha_page(session, base_url, auth_token)
            continue

        with open("captcha.png", "wb") as f:
            f.write(r_img.content)
        log_debug("    验证码已保存: captcha.png（%d 字节）", len(r_img.content))

        answer = _ocr_recognize(ocr, r_img.content)
        if not answer:
            if captcha_answer:
                answer = captcha_answer
                log_warning("    OCR 失败，使用手动验证码: %s", answer)
            else:
                log_warning("    OCR 失败，无手动验证码，跳过第%d次", attempt)
                r_cap, idhash, auth_token = _refresh_captcha_page(session, base_url, auth_token)
                continue

        if _submit_captcha_login(session, base_url, r_cap, idhash, auth_token,
                                 username, password, answer):
            log_info("    ✓ 验证码登录成功！（第%d次）", attempt)
            return _verify_login(session, base_url)

        if attempt < 5:
            log_debug("    重新获取验证码页面（第%d次尝试后）...", attempt)
            r_cap, idhash, auth_token = _refresh_captcha_page(session, base_url, auth_token)

    _log_login_evidence("captcha", "验证码循环结束时的页面", r_cap)
    raise LoginRetryable("验证码识别率过低，请检查 captcha.png 后手动重试")


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


# ── MCBBSPoster 类（编程接口）──────────────────────

class MCBBSPoster:
    """MCBBS 发帖器，支持批量发帖时复用登录会话"""

    def __init__(self, config: dict = None):
        if config is None:
            config = load_poster_config()
        self.base_url = config["base_url"]
        self.forum_fid = config["forum_fid"]
        self.username = config["username"]
        self.password = config["password"]
        self.captcha_answer = config.get("captcha_answer", "")
        self.sortid_map = config.get("sortid_map", {})
        self.login_retry = dict(config.get("login_retry") or {})
        self.highlight = _merge_highlight(config.get("highlight"))
        self.session = None
        self.formhash = None

    def _login_retry_policy(self) -> tuple[int, int]:
        """登录重试次数与间隔（秒），默认值来自 DEFAULT_LOGIN_RETRY。"""
        attempts, wait = DEFAULT_LOGIN_RETRY["max_attempts"], DEFAULT_LOGIN_RETRY["wait_seconds"]
        with contextlib.suppress(TypeError, ValueError):
            attempts = int(self.login_retry.get("max_attempts", attempts))
        with contextlib.suppress(TypeError, ValueError):
            wait = int(self.login_retry.get("wait_seconds", wait))
        return max(attempts, 1), max(wait, 0)

    def login(self) -> bool:
        """登录 MCBBS，返回是否成功。

        站点会概率性地要求验证码，验证码页的结构也不一定符合标准模板。一次失败
        就整轮不发布代价太大，所以登录失败（含网络抖动）时换一个新会话重试；
        凭证被站点明确拒绝则立即抛出，不再加重失败计数。重试参数见 retry.login。
        """
        if not self.username or not self.password:
            raise RuntimeError("未配置 MCBBS 账号密码")

        # 补高亮可能已经登录过了（它只在真要动手时才登录），这里别再跑一遍验证码
        if self.session is not None and self.formhash:
            log_info("[*] 复用已有的 MCBBS 会话")
            return True

        max_attempts, wait_seconds = self._login_retry_policy()
        attempt = 0
        while True:
            attempt += 1
            suffix = "" if attempt == 1 else f"（第 {attempt}/{max_attempts} 次）"
            log_info(f"[*] 登录 MCBBS: {self.username}{suffix}")
            try:
                return self._login_once()
            except (LoginRetryable, requests.RequestException) as e:
                if attempt >= max_attempts:
                    raise
                log_warning(f"    ⚠ 登录未完成: {e}；{wait_seconds} 秒后换新会话重试")
                time.sleep(wait_seconds)

    def _login_once(self) -> bool:
        """走一遍完整登录流程（新建会话）；失败时抛可重试/不可重试的登录错误。"""
        self.session = _make_session()

        r = self.session.get(f"{self.base_url}/member.php?mod=logging&action=login",
                             timeout=LOGIN_TIMEOUT)
        r.raise_for_status()
        try:
            formhash = extract_formhash(r.text)
        except ValueError as e:
            _log_login_evidence("login", "登录页", r)
            raise LoginRetryable(f"登录页缺少 formhash（{e}），可能是风控页或站点改版") from None
        loginhash = extract_loginhash(r.text)

        login_url = (
            f"{self.base_url}/member.php?mod=logging&action=login"
            f"&loginsubmit=yes&loginhash={loginhash}"
        )
        data = {
            "formhash": formhash, "referer": "./",
            "username": self.username, "password": self.password,
            "questionid": "0", "answer": "", "cookietime": "2592000",
        }
        r = self.session.post(login_url, data=data, timeout=LOGIN_TIMEOUT)

        if "欢迎您回来" in r.text:
            self.formhash = _verify_login(self.session, self.base_url)
            log_info("    ✓ 登录成功！")
            return True

        if "验证码" in r.text:
            self.formhash = _login_with_captcha(
                self.session, self.base_url,
                self.username, self.password, self.captcha_answer, r
            )
            return True

        err = re.search(r'id="messagetext"[^>]*>(.*?)</div>', r.text, re.S)
        if not err:
            _log_login_evidence("login", "登录结果", r)
            raise LoginRetryable("登录结果无法识别（页面里没有提示信息），可能命中风控页")
        err_msg = re.sub(r"<[^>]+>", "", err.group(1)).strip()[:200]
        # 凭证被拒就停手；其余提示（来路不正确、表单验证串不符等）多是临时状态，值得重试
        if _is_permanent_login_error(err_msg):
            raise LoginRejected(f"登录失败: {err_msg}")
        _log_login_evidence("login", "登录结果", r)
        raise LoginRetryable(f"登录失败（可重试）: {err_msg}")

    def _upload(self, file_path: str, upload_filename: str, mime_type: str,
                extra_data: dict = None) -> str:
        """内部上传方法，封装重试逻辑。extra_data 用于图片上传的 type:image 参数。"""
        upload_url = (
            f"{self.base_url}/misc.php?mod=swfupload&action=swfupload"
            f"&operation=upload&fid={self.forum_fid}"
        )
        referer = f"{self.base_url}/forum.php?mod=post&action=newthread&fid={self.forum_fid}"
        aid = ""
        for attempt in range(1, 4):
            r = self.session.get(referer)
            r.raise_for_status()
            hash_m = re.search(r'"hash"\s*:\s*"([a-f0-9]+)"', r.text)
            uid_m = re.search(r'"uid"\s*:\s*"(\d+)"', r.text)
            if not hash_m or not uid_m:
                raise RuntimeError("无法提取上传参数")

            self.session.headers.update({"Referer": referer})
            data = {"uid": uid_m.group(1), "hash": hash_m.group(1)}
            if extra_data:
                data.update(extra_data)
            with open(file_path, "rb") as f:
                r_up = self.session.post(
                    upload_url,
                    files={"Filedata": (upload_filename, f, mime_type)},
                    data=data,
                    timeout=30,
                )
            self.session.headers.pop("Referer", None)

            aid = r_up.text.strip()
            if aid.isdigit():
                return aid

            if attempt < 3:
                log_warning(f"    ⚠ 第{attempt}次上传失败，2秒后重试...")
                time.sleep(2)

        raise RuntimeError(f"上传失败: {aid[:200]}")

    def upload_file(self, file_path: str, mime_type: str = None) -> str:
        """上传普通文件（非图片附件），返回附件 ID。"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        filename = os.path.basename(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        if mime_type is None:
            mime_map = {
                ".json": "text/plain",   # Discuz swfupload 拒绝 application/json
                ".txt": "text/plain",
                ".md": "text/plain",     # Discuz 不接受 text/markdown
                ".pdf": "application/pdf",
                ".zip": "application/zip",
            }
            mime_type = mime_map.get(ext, "application/octet-stream")

        log_info(f"    上传文件: {filename}")

        # Discuz swfupload 服务端校验文件扩展名
        # 上传时改扩展名为 .txt 绕过限制（内容不变，用户下载后改名即可）
        allowed_exts = {".chm", ".pdf", ".zip", ".7z", ".7zip", ".rar", ".tar",
                        ".gz", ".bzip2", ".gif", ".jpg", ".jpeg", ".png", ".jar",
                        ".txt", ".webp", ".log", ".conf", ".mcworld", ".mcpack",
                        ".lang", ".bmp"}
        upload_filename = filename
        if ext not in allowed_exts:
            upload_filename = os.path.splitext(filename)[0] + ".txt"
            mime_type = "text/plain"
            log_info(f"    (扩展名 {ext} 不被允许，上传时改用 {upload_filename})")

        aid = self._upload(file_path, upload_filename, mime_type)
        log_info(f"    ✓ 文件上传成功！aid={aid}")
        return aid

    def upload_image(self, image_path: str) -> str:
        """上传图片，返回附件 ID"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        log_info(f"    上传图片: {os.path.basename(image_path)}")
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"
        }
        mime = mime_map.get(ext, "image/jpeg")
        filename = os.path.basename(image_path)
        aid = self._upload(image_path, filename, mime, extra_data={"type": "image"})
        log_info(f"    ✓ 上传成功！aid={aid}")
        return aid

    def post_thread(self, title: str, message: str,
                    attachment_ids: list = None, sortid: int = None) -> PostResult:
        """发帖，返回 PostResult（帖子地址、帖子 ID、是否需审核）"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        log_info(f"    标题: {title}")
        log_info(f"    正文长度: {len(message)} 字符")

        newthread_url = (
            f"{self.base_url}/forum.php?mod=post&action=newthread&fid={self.forum_fid}"
        )
        r = self.session.get(newthread_url)
        r.raise_for_status()

        if "绑定手机号" in r.text:
            raise RuntimeError("MCBBS 要求绑定手机号，请先在网页端完成绑定")

        uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
        if not uid or uid.group(1) == "0":
            raise RuntimeError("发帖前验证失败：未登录")

        post_formhash = extract_formhash(r.text)
        post_url = (
            f"{self.base_url}/forum.php?mod=post&action=newthread"
            f"&fid={self.forum_fid}&extra=&topicsubmit=yes"
        )
        data = {
            "formhash": post_formhash,
            "posttime": str(int(time.time())),
            "wysiwyg": "1",
            "subject": title,
            "message": message,
            "usesig": "1",
            "allownoticeauthor": "1",
        }
        if sortid is not None:
            data["typeid"] = str(sortid)
        if attachment_ids:
            for aid in attachment_ids:
                data[f"attachnew[{aid}][description]"] = ""

        self.session.headers.update({"Referer": newthread_url})
        try:
            r = self.session.post(post_url, data=data, allow_redirects=False)
        finally:
            self.session.headers.pop("Referer", None)
        r.raise_for_status()

        return self._parse_post_response(r)

    def _parse_post_response(self, response) -> PostResult:
        """解析发帖响应：帖子 URL、帖子 ID，以及是否进了审核。"""
        html = getattr(response, "text", "") or ""
        headers = getattr(response, "headers", None) or {}
        needs_audit = "需要审核" in html or "通过审核后才能显示" in html

        sources = [_find_redirect(html), headers.get("Location", ""), getattr(response, "url", "")]
        for source in sources:
            tid = _extract_tid(source)
            if tid:
                url = f"{self.base_url}/thread-{tid}-1-1.html"
                log_info(f"    ✓ 发帖成功{'（需审核）' if needs_audit else ''}: {url}")
                return PostResult(url, tid, needs_audit)

        if needs_audit:
            # 审核中的帖子通常没有可用的跳转地址，帖子 ID 只能过后从「我的帖子」里补
            log_info("    ✓ 发帖成功（需审核，响应里没有帖子 ID，稍后按列表补齐）")
            return PostResult("(需审核)", None, True)

        err = re.search(r'id="messagetext"[^>]*>(.*?)</div>', html, re.S)
        if err:
            raise RuntimeError(f"发帖失败: {re.sub(r'<[^>]+>', '', err.group(1)).strip()[:200]}")

        raise RuntimeError(f"发帖结果不明: {getattr(response, 'url', '')}")

    def _ensure_formhash(self) -> str:
        """取一个可用的 formhash：登录后首页会下发，换会话或久未动手时得重取。"""
        if self.formhash:
            return self.formhash
        r = self.session.get(f"{self.base_url}/forum.php", timeout=LOGIN_TIMEOUT)
        r.raise_for_status()
        self.formhash = extract_formhash(r.text)
        return self.formhash

    def highlight_thread(self, tid: int, color: str, bold: bool = False,
                         fid: int | None = None) -> tuple[bool, str]:
        """给帖子设置标题高亮，返回 (是否成功, 失败原因)。

        Discuz 只允许版块版主操作，且对审核中（displayorder < 0）的帖子直接跳过；
        两种情况都会落到 errorhandle，这里把站点提示原样带出去，由调用方决定重试
        还是放弃。加粗取消也走同一条路：同色 + 样式位 0 = 保留颜色、去掉加粗。
        """
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")
        color_index = highlight_color_index(color)
        if not color_index:
            return False, f"色值不在 Discuz 色板内: {color}"

        forum_id = fid or self.forum_fid
        thread_url = f"{self.base_url}/thread-{tid}-1-1.html"
        payload = {
            "formhash": self._ensure_formhash(),
            "fid": str(forum_id),
            # 没有 redirect 时站点不会回 succeedhandle，成功也会被当成失败
            "redirect": thread_url,
            "handlekey": "mods",
            "moderate[]": str(tid),
            "operations[]": "highlight",
            "highlight_color": str(color_index),
            "highlight_style[1]": "1" if bold else "0",
            "highlight_style[2]": "0",
            "highlight_style[3]": "0",
            "highlight_bgcolor": "",
            "expirationhighlight": "",
        }
        action_url = (
            f"{self.base_url}/forum.php?mod=topicadmin&action=moderate"
            f"&optgroup=1&modsubmit=yes&infloat=yes&handlekey=mods&inajax=1"
        )
        # submitcheck 要求表单提交的 Referer 与站点同源（或为空），这里一并对齐
        self.session.headers.update({"Referer": thread_url})
        try:
            response = self.session.post(action_url, data=payload, timeout=LOGIN_TIMEOUT)
        finally:
            self.session.headers.pop("Referer", None)
        response.raise_for_status()

        ok, reason = _parse_moderation_result(response.text)
        if not ok:
            log_warning(f"    ⚠ 高亮未生效（tid={tid}）: {reason}")
        return ok, reason

    def fetch_my_threads(self, pages: int = 2) -> list[MyThread]:
        """拉「我的帖子」列表，返回 MyThread 列表；待审核、回收站的帖子也在里面。

        待审核/回收站的帖子按 tid 倒序排在最前，但我们可能又发了几篇，所以留一点
        余量（默认两页）。解析不出内容时把页面落盘，方便对上站点的自定义模板。
        """
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")
        threads: list[MyThread] = []
        seen: set[int] = set()
        for page in range(1, max(pages, 1) + 1):
            url = f"{self.base_url}/home.php?mod=space&do=thread&view=me&type=thread"
            if page > 1:
                url += f"&page={page}"
                time.sleep(1)  # 翻页放慢一点，别把列表页当接口刷
            response = self.session.get(url, timeout=LOGIN_TIMEOUT)
            response.raise_for_status()
            page_threads = _parse_my_threads(response.text)
            if not page_threads:
                if page == 1:
                    path = _dump_threads_html(response.text)
                    log_warning("    ⚠ 「我的帖子」没有解析出任何帖子（页面已存: %s）", path or "落盘失败")
                break
            for item in page_threads:
                if item.tid not in seen:
                    seen.add(item.tid)
                    threads.append(item)
        return threads

    def fetch_forum_threads(self, pages: int = 3) -> list[ForumThread]:
        """读版块列表里每篇帖子的当前高亮状态（公开页面，不需要登录态）。

        高亮样式只渲染在版块列表里，所以判断「谁现在是加粗的」只能看这里；
        取前几页就够——版本资讯都在最新发布的那几页。
        """
        threads: list[ForumThread] = []
        seen: set[int] = set()
        with requests.Session() as session:
            session.headers.update({"User-Agent": UA})
            for page in range(1, max(pages, 1) + 1):
                if page > 1:
                    time.sleep(1)  # 翻页放慢一点，别把列表页当接口刷
                url = f"{self.base_url}/forum.php?mod=forumdisplay&fid={self.forum_fid}&page={page}"
                response = session.get(url, timeout=LOGIN_TIMEOUT)
                response.raise_for_status()
                page_threads = _parse_forum_threads(response.text)
                if page == 1 and not page_threads:
                    path = _dump_threads_html(response.text)
                    log_warning("    ⚠ 版块列表没有解析出任何帖子（页面已存: %s）", path or "落盘失败")
                    break
                if not page_threads:
                    break
                for item in page_threads:
                    if item.tid not in seen:
                        seen.add(item.tid)
                        threads.append(item)
        return threads

    def process_pending_highlights(self, records_path: str, only: str | None = None) -> dict:
        """补上待高亮的帖子：先匿名探测过审，需要时才登录动手。

        站点对审核中的帖子拒绝高亮，所以探测这一步必须先做：不登录就能排除掉
        绝大多数「还没过审」的项，省掉一轮验证码。返回统计供日志汇总。
        `only` 限定只处理某一条记录（单篇验证命令用），别顺手改到别的帖子。
        """
        stats = {"pending": 0, "highlighted": 0, "unbolded": 0, "gave_up": 0,
                 "failed": 0, "dropped": 0, "stale": 0}
        records = load_thread_records(records_path)
        before = _records_digest(records)
        now = time.time()
        try:
            if not self.highlight.get("enabled"):
                # 关掉高亮之后不该再登录动论坛；待办原样留着，打开后继续
                log_info("[高亮] 配置里已关闭高亮（mcbbs.highlight.enabled），本轮不处理")
                return stats
            pending = {stem: rec for stem, rec in records.items()
                       if rec.get("state") == "pending" and (only is None or stem == only)}
            stats["pending"] = len(pending)
            if pending:
                self._process_ready(records, pending, stats, now)
        finally:
            # 探测/登记结果无论如何都要落盘：登录失败也不该把上一轮的进展丢掉
            pruned = self._prune_records(records)
            if pruned or _records_digest(records) != before:
                self._save_records(records_path, records)

        log_info("[高亮] 待补 %d 篇 | 已设置 %d | 取消加粗 %d | 放弃 %d | 失败 %d | 跳过 %d",
                 stats["pending"], stats["highlighted"], stats["unbolded"],
                 stats["gave_up"], stats["failed"], stats["dropped"])
        if stats["stale"]:
            log_info("[高亮] 另有 %d 篇超过 %d 天未补上，已放弃",
                     stats["stale"], self._max_age_days())
        return stats

    def _process_ready(self, records: dict, pending: dict, stats: dict, now: float) -> None:
        """把 pending 里已经可以动手的挑出来，登录后提交高亮。"""
        exclude = self.highlight.get("exclude_forums") or []
        missing = {}
        for stem, rec in pending.items():
            if rec.get("tid"):
                continue
            if self._too_old(rec, now):
                # 年龄先判：过期记录不值得再为它跑一次「我的帖子」
                self._retire(rec, stats, stale=True)
                continue
            missing[stem] = rec
        if missing:
            self._resolve_tids(records, missing, now)

        ready: list[str] = []
        for stem, rec in pending.items():
            tid = rec.get("tid")
            if not tid:
                if self._expired(rec, now):
                    self._retire(rec, stats)
                continue
            if self._too_old(rec, now):
                self._retire(rec, stats, stale=True)
                continue
            if self._expired(rec, now):
                # 等审核已经超过 max_age_hours：不再为这个死帖花一次请求
                self._retire(rec, stats)
                continue
            if not highlight_color_index(rec.get("color", "")):
                # 色值不在色板里：登录提交也一定被拒，别拿它消耗重试次数
                self._retire(rec, stats, stale=False,
                             reason=f"色值不在 Discuz 色板内: {rec.get('color') or '未配置'}")
                continue
            probe = probe_thread(self.base_url, int(tid))
            if probe.visible is None:
                rec["last_error"] = "过审探测失败（网络异常，下轮重试）"
                rec["updated_at"] = _timestamp()
                continue
            if not probe.visible:
                # 还在审核里不算一次失败：审核是人工的，多久都有可能
                rec["last_error"] = "仍在审核中"
                continue
            skipped = _forum_excluded(probe.crumb, probe.forum_fid, exclude)
            if skipped:
                # 被移动/删除到回收区、或者落在排除版块里的帖子，永远不该上高亮
                rec["state"] = "dropped"
                rec["last_error"] = skipped
                rec["updated_at"] = _timestamp()
                stats["dropped"] += 1
                log_info("    → 跳过高亮（%s）: %s", skipped, rec.get("title", stem))
                continue
            ready.append(stem)

        if not ready:
            return
        if not self.session:
            self.login()
        for stem in ready:
            rec = records[stem]
            try:
                ok, reason = self.highlight_thread(
                    int(rec["tid"]), rec.get("color", ""),
                    bold=bool(rec.get("bold")), fid=rec.get("fid"),
                )
            except requests.RequestException as e:
                ok, reason = False, f"高亮请求失败: {e}"
            if not ok:
                if self._count_attempt(rec, reason, now):
                    stats["gave_up"] += 1
                else:
                    stats["failed"] += 1
                continue
            rec["state"] = "done"
            rec["highlighted_at"] = _timestamp()
            rec["updated_at"] = _timestamp()
            rec["last_error"] = ""
            stats["highlighted"] += 1
        # 收敛放在最后、整个 sweep 只读一次版块列表：逐篇去读会让请求数乘以篇数，
        # 而且「最后处理的那篇」会变成赢家，而不是真正最新的那篇
        stats["unbolded"] += self._converge_after_sweep(records)

    def _retire(self, rec: dict, stats: dict, *, stale: bool = False,
                reason: str = "") -> None:
        """把一条记录判成放弃，并记到统计里。"""
        rec["state"] = "gave_up"
        if stale:
            rec["last_error"] = f"超过 {self._max_age_days()} 天仍未补上高亮，不再处理"
            stats["stale"] += 1
            log_info("    → 已过期，不再补高亮: %s", rec.get("title") or "")
        else:
            rec["last_error"] = reason or "等待审核超时，已放弃"
            stats["gave_up"] += 1
            log_warning("    ⚠ 放弃补高亮: %s（%s）", rec.get("title") or "", rec["last_error"])
        rec["updated_at"] = _timestamp()

    def _converge_after_sweep(self, records: dict) -> int:
        """补高亮之后收敛一次加粗：只取消「同组里比最新那篇更旧的」加粗帖。"""
        try:
            rows = self.fetch_forum_threads(pages=3)
        except requests.RequestException as e:
            log_warning("    ⚠ 读版块列表失败，本轮不取消旧版加粗: %s", e)
            return 0
        if not rows:
            return 0
        return self._converge_bold(records, rows, allow_bold=False)["unbolded"]

    def _resolve_tids(self, records: dict, missing: dict, now: float) -> None:
        """从「我的帖子」里补上发帖时没拿到的帖子 ID。

        先按标题精确匹配；匹配不上再认领列表里唯一一条「比所有已知记录都新」的帖子
        （标题被版主改过时靠这条兜底）。查询本身失败不算一次重试：网络抖动、没登录
        都不该让记录滑向 gave_up。
        """
        if not self.session:
            # 「我的帖子」要登录态；真要补高亮也得登录，所以这里先登，登不上就当本轮
            # 查不到（不记账），别让验证码问题把记录推向 gave_up
            try:
                self.login()
            except (requests.RequestException, RuntimeError) as e:
                log_warning("    ⚠ 补齐帖子 ID 需要登录，本轮跳过（不算失败）: %s", e)
                return
        try:
            threads = self.fetch_my_threads()
        except (requests.RequestException, RuntimeError) as e:
            log_warning("    ⚠ 拉取「我的帖子」失败（下轮重试，不算失败）: %s", e)
            return
        if not threads:
            log_warning("    ⚠ 「我的帖子」里没有任何可用条目，无法补齐帖子 ID")
            return

        # 删除线（回收站）的行不能拿来认领：要么已删、要么是撤掉 AI 翻译的旧稿；
        # 落在排除版块里的同理，否则补到 tid 之后又会被 dropped 掉
        exclude = self.highlight.get("exclude_forums") or []
        alive = [item for item in threads
                 if not item.recy
                 and not _forum_excluded(item.forum_name, item.forum_fid, exclude)]
        by_title = {_normalize_title(item.title): item.tid for item in alive}
        known = {rec.get("tid") for rec in records.values() if rec.get("tid")}
        newest = max((item.tid for item in alive), default=0)
        resolved = 0
        for stem, rec in missing.items():
            tid = by_title.get(_normalize_title(rec.get("title", "")))
            source = "标题匹配"
            if not tid and len(missing) == 1 and newest and newest not in known:
                # 只有「只差这一条、列表里也只有一个没被认领的新帖」时才猜；
                # 多条待补时这么认领会把 A 的 tid 安到 B 头上
                tid, source = newest, "列表新增兜底"
            if not tid:
                continue
            rec["tid"] = tid
            rec["url"] = f"{self.base_url}/thread-{tid}-1-1.html"
            rec["tid_source"] = source
            rec["updated_at"] = _timestamp()
            known.add(tid)
            resolved += 1
            log_info("    → 补齐帖子 ID: %s → tid=%s（%s）", stem, tid, source)
        if not resolved:
            # 列表查到了但一条都对不上：这才是真正「找不到这篇帖子」
            for stem, rec in missing.items():
                if self._count_attempt(rec, "「我的帖子」里还没找到这篇帖子", now):
                    log_warning("    ⚠ 放弃补高亮（找不到帖子）: %s", rec.get("title") or stem)

    def _bold_group_keys(self) -> set[tuple[int, str]]:
        """`bold_types` 覆盖到的 (分类, 版本线) 组合，收敛只在这些组里发生。

        只按分类（typeid）过滤不够：`java_release` 和 `java_snapshot` 都落在 typeid 4，
        但正式版和测试版是两条独立的版本线。用户把 `bold_types` 收窄成「只要正式版」
        之后，按分类过滤仍会去加粗测试版的最新帖、取消测试版旧帖的加粗 —— 配置等于
        没生效；块讯之类的分类即使被人手工加粗，也不该因为我们给别的帖上色而被顺手取消。
        """
        keys: set[tuple[int, str]] = set()
        for news_type in self.highlight.get("bold_types") or []:
            typeid = self.sortid_map.get(news_type)
            family = _bold_family("", news_type)
            if isinstance(typeid, int) and family:
                keys.add((typeid, family))
            else:
                log_debug("    · bold_types 里的 %s 没配上 sortid_map 分类，不参与收敛", news_type)
        return keys

    def _converge_bold(self, records: dict, rows: list[ForumThread], *,
                       dry_run: bool = False, allow_bold: bool = True) -> dict:
        """按 (分类, 版本线) 把加粗收敛成每组只有最新一篇，返回统计。

        版规是「最新版本加粗」，而正式版和测试版是两条独立的版本线（正式版红色、
        测试版绿色），Java 版和基岩版又各算各的——一共 4 个「最新」。所以收敛按
        (typeid, 正式版/测试版) 分组，跨组永不互相取消。

        必须以整张列表一次算完再动手：逐篇「谁刚加粗谁就是赢家」的写法下，同一批里
        后处理的旧版会把先处理的新版取消掉。判断依据是列表现状（font-weight）而不是
        本地记录——历史帖可能没进过记录，也可能被人改过。

        `allow_bold=False` 只取消更旧的加粗、不补加粗（补高亮流程用这个模式）：最新
        那篇还没加粗的组整组不动，最新那篇已经加粗的组仍会清掉多出来的旧加粗 ——
        所以一次补高亮也可能顺带收敛别的版本线，这是「一条线只留一篇加粗」的既定
        语义，而不是只动本次碰过的那一组。补加粗失败时整组保持原样，避免把一条版本线
        弄成「一篇都不加粗」。
        """
        stats = {"groups": 0, "bolded": 0, "unbolded": 0, "failed": 0, "skipped": 0}
        group_keys = self._bold_group_keys()
        groups: dict[tuple[int, str], list[ForumThread]] = {}
        for row in rows or []:
            family = _bold_family(row.title, (self._record_for_tid(records, row.tid) or {})
                                  .get("news_type", ""))
            if not family:
                stats["skipped"] += 1
                log_debug("    · 认不出版本线，跳过: %s（tid=%s）", row.title, row.tid)
                continue
            if (row.typeid, family) not in group_keys:
                stats["skipped"] += 1
                log_debug("    · 这条版本线不在 bold_types 里，跳过: %s（tid=%s）",
                          row.title, row.tid)
                continue
            groups.setdefault((row.typeid, family), []).append(row)

        plans: list[tuple[str, ForumThread, str, list[ForumThread]]] = []
        for (typeid, family), group in sorted(groups.items()):
            stats["groups"] += 1
            group.sort(key=lambda item: item.tid)  # tid 递增 ≈ 发帖顺序，最后一篇最新
            newest = group[-1]
            label = f"{newest.type_name or f'typeid={typeid}'}／{_FAMILY_NAMES[family]}"
            if not newest.bold and not allow_bold:
                # 补高亮流程不负责补加粗：最新那篇都没加粗，这组就不该动
                continue
            older = [row for row in group if row.tid != newest.tid and row.bold]
            if not older and newest.bold:
                continue
            color = self._family_color(newest, group, records)
            if not color:
                stats["skipped"] += 1
                log_warning("    ⚠ %s 认不出该用什么颜色，跳过: %s（tid=%s）",
                            label, newest.title, newest.tid)
                continue
            log_info("    · %s: 最新是 tid=%s（%s）", label, newest.tid,
                     "已加粗" if newest.bold else "待加粗")
            plans.append((label, newest, color, older))

        if not plans:
            return stats
        if dry_run:
            for label, newest, color, older in plans:
                if not newest.bold:
                    log_info("  [Dry Run] %s | 加粗 | tid=%s | %s | %s",
                             label, newest.tid, color, newest.title)
                for row in older:
                    log_info("  [Dry Run] %s | 取消加粗 | tid=%s | %s | %s",
                             label, row.tid, row.color or color, row.title)
            return stats

        if not self.session:
            self.login()
        for label, newest, color, older in plans:
            if not newest.bold:
                ok, reason = self._submit_highlight(records, newest, color, bold=True)
                if not ok:
                    # 最新那篇没加粗成功，就别动同组的旧帖，否则整组一个都不加粗
                    stats["failed"] += 1
                    log_warning("    ⚠ %s 加粗失败，本组保持原样: %s（%s）",
                                label, newest.title, reason)
                    continue
                stats["bolded"] += 1
                log_info("    → 已加粗: %s（tid=%s，%s）", newest.title, newest.tid, label)
            for row in older:
                # 颜色只认站点现值或本地记录；认不出就别动它——换成配置里的兜底色
                # 等于顺手改了人家的颜色
                record = self._record_for_tid(records, row.tid) or {}
                row_color = row.color or record.get("color", "")
                if not highlight_color_index(row_color):
                    stats["skipped"] += 1
                    log_warning("    ⚠ 认不出该帖的颜色，保留原样: %s（tid=%s）",
                                row.title, row.tid)
                    continue
                ok, reason = self._submit_highlight(records, row, row_color, bold=False)
                if ok:
                    stats["unbolded"] += 1
                    log_info("    → 已取消旧版加粗: %s（tid=%s）", row.title, row.tid)
                else:
                    stats["failed"] += 1
                    log_warning("    ⚠ 取消旧版加粗失败: %s（%s）", row.title, reason)
        return stats

    def _submit_highlight(self, records: dict, row: ForumThread, color: str,
                          bold: bool) -> tuple[bool, str]:
        """提交一次高亮并把结果同步进本地记录；失败时只记原因，不改记录里的颜色。"""
        try:
            ok, reason = self.highlight_thread(row.tid, color, bold=bold)
        except requests.RequestException as e:
            ok, reason = False, f"请求失败: {e}"
        record = self._record_for_tid(records, row.tid)
        if record is None and bold and ok:
            # 站点上有条目但本地没登记（例如人工发的版本帖），补一条便于后续对账
            record = {"tid": row.tid, "fid": self.forum_fid, "typeid": row.typeid,
                      "url": f"{self.base_url}/thread-{row.tid}-1-1.html",
                      "title": row.title, "forum_name": "", "news_type": "manual",
                      "color": color, "bold": True, "state": "done", "posted_at": "",
                      "registered_at": _timestamp(),
                      "attempts": 0, "last_error": "", "tid_source": "加粗收敛"}
            records[f"tid_{row.tid}"] = record
        if record is not None:
            if ok:
                record["bold"] = bold
                record["color"] = color or record.get("color", "")
                record["last_error"] = ""
                if bold:
                    # 上过色就不再是「历史未处理」：免得 --highlight-legacy 再刷一遍
                    record["state"] = "done"
                    record["highlighted_at"] = _timestamp()
            else:
                record["last_error"] = reason
            record["updated_at"] = _timestamp()
        return ok, reason

    def reconcile_bold(self, records_path: str, pages: int = 5, dry_run: bool = False) -> dict:
        """按「分类 + 版本线」把加粗收敛成每类每线只有最新一篇，一共 4 组。

        自动流程只在补高亮成功后顺带取消同线旧帖的加粗，够不着历史遗留和人工改过的
        状态（例如 Java版资讯 里 26.3 正式版和 26.4 快照 8 同时加粗）。这条命令以版块
        列表为准整体对一次账，可以反复跑。默认读 5 页：只读 3 页时第三页之后的加粗帖
        既扫不到也取消不了，等于「对完账还剩两篇加粗」。

        返回的 `ok` 说明这次对账有没有失败，CLI 据此决定退出码。
        """
        stats = {"ok": False, "groups": 0, "bolded": 0, "unbolded": 0,
                 "failed": 0, "skipped": 0}
        records = load_thread_records(records_path)
        try:
            rows = self.fetch_forum_threads(pages=pages)
        except requests.RequestException as e:
            log_error("[加粗收敛] 读版块列表失败: %s", e)
            return stats
        if not rows:
            log_error("[加粗收敛] 版块列表没有解析出任何帖子")
            return stats

        stats.update(self._converge_bold(records, rows, dry_run=dry_run))
        stats["ok"] = True
        if dry_run:
            log_info("[Dry Run] 加粗收敛共 %d 个动作（%d 组），未改论坛",
                     stats["bolded"] + stats["unbolded"], stats["groups"])
            return stats
        self._save_records(records_path, records)
        if stats["failed"]:
            stats["ok"] = False
        log_info("[加粗收敛] %d 组 | 加粗 %d | 取消加粗 %d | 失败 %d | 跳过 %d",
                 stats["groups"], stats["bolded"], stats["unbolded"],
                 stats["failed"], stats["skipped"])
        return stats

    def _family_color(self, newest: ForumThread, group: list[ForumThread], records: dict) -> str:
        """给一组版本帖定加粗时要用的色值：已经上过色的那份最可信。"""
        record = self._record_for_tid(records, newest.tid) or {}
        candidates = [newest.color, record.get("color", "")]
        candidates += [row.color for row in group if row.tid != newest.tid]
        for color in candidates:
            if highlight_color_index(color):
                return color
        return ""

    @staticmethod
    def _record_for_tid(records: dict, tid: int) -> dict | None:
        """按帖子 ID 找本地记录（历史帖很可能没有记录，返回 None）。"""
        for record in records.values():
            if record.get("tid") == tid:
                return record
        return None

    def _count_attempt(self, rec: dict, reason: str, now: float) -> bool:
        """记一次失败尝试，返回是否该放弃（次数或等待时间超限）。"""
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["last_error"] = reason
        rec["updated_at"] = _timestamp()
        max_attempts = int(self.highlight.get("max_attempts") or 0)
        if (max_attempts and rec["attempts"] >= max_attempts) or self._expired(rec, now):
            rec["state"] = "gave_up"
            return True
        return False

    def _max_age_days(self) -> int:
        """多久以前的帖子就不再管（0 表示不限）。"""
        value = self.highlight.get("max_age_days")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    def _stale_artifact(self, released: float | None) -> bool:
        """产物的发布时间是否已经超出处理期限；时间未知时当作没过期。"""
        max_days = self._max_age_days()
        if released is None or not max_days:
            return False
        return (time.time() - released) > max_days * 86400

    def _stale_thread(self, site_row, released: float | None) -> bool:
        """帖子是否过期：优先看版块列表上的发帖日期，取不到才退回产物日期。

        必须以帖子日期为准：旧文章被重新发一遍时产物日期很老，但帖子是今天发的，
        照样该上色。版块列表默认按最后回复排序，所以不能拿行的位置当代替。
        """
        posted = getattr(site_row, "posted", "") or ""
        started = _parse_timestamp(f"{posted}T00:00:00") if posted else released
        if started is None:
            return False
        return self._stale_artifact(started)

    def _drop_stale_legacy(self, records: dict, save_dir: str, site_rows: dict) -> int:
        """删掉已经过期的 legacy 登记，返回条数。

        legacy 记录的本意是「历史帖还没上色，随时可以提升」；老到超出窗口之后
        这条记录就只是一行永远用不上的数据。两种记录不动：待办（pending、legacy 里
        加粗中的），以及**站点上仍然加粗的**——本地 bold 可能是旧的，删掉就再也找不到
        这条加粗帖了。
        """
        dropped = 0
        for stem in list(records):
            record = records[stem]
            if record.get("state") != "legacy" or record.get("bold"):
                continue
            site_row = site_rows.get(record.get("tid"))
            if site_row is not None and site_row.bold:
                continue
            if self._stale_thread(site_row, _artifact_release_time(save_dir, stem)):
                records.pop(stem, None)
                dropped += 1
        return dropped

    def _too_old(self, rec: dict, now: float) -> bool:
        """记录是否已经超出处理期限；算不出年龄的一律当作没过期。"""
        max_days = self._max_age_days()
        started = _record_time(rec)
        if not max_days or started is None:
            return False
        return (now - started) > max_days * 86400

    def _expired(self, rec: dict, now: float) -> bool:
        """记录是否已经超过「等审核」的上限（从登记/提升那一刻算，不是发帖时间）。"""
        max_hours = self.highlight.get("max_age_hours") or 0
        started = _waiting_since(rec)
        if not max_hours or started is None:
            return False
        return (now - started) > max_hours * 3600

    def _prune_records(self, records: dict, now: float | None = None) -> int:
        """删掉过期且不再需要的记录，返回删除条数。

        记录留着只为了「待办」和「以后还能撤销/对照」；一旦帖子老到不再处理，留一条
        记录只会让文件越长越大、让 --highlight-status 充满「没找到」。三条豁免：
        `pending` 是待办；加粗中的帖子要留着才能随时取消加粗；算不出年龄的老记录
        不敢删。给缺时间戳的记录补一次 registered_at，从这一刻开始算年龄。
        """
        moment = now if now is not None else time.time()
        max_days = self._max_age_days()
        dropped = 0
        for stem in list(records):
            rec = records[stem]
            if not rec.get("registered_at") and not rec.get("posted_at"):
                rec["registered_at"] = rec.get("updated_at") or _timestamp()
            if not max_days or rec.get("state") == "pending" or rec.get("bold"):
                continue
            if self._too_old(rec, moment):
                records.pop(stem, None)
                dropped += 1
        return dropped

    def _save_records(self, records_path: str, records: dict) -> None:
        self._prune_records(records)
        try:
            save_thread_records(records_path, records)
        except OSError:
            log_warning("    ⚠ 帖子记录写入失败: %s", records_path, exc_info=True)

    def index_history(self, save_dir: str, pages: int = 15, dry_run: bool = False) -> dict:
        """把「我的帖子」里对得上本地产物的历史帖登记成 legacy，不动论坛。

        历史帖从来没有高亮记录。如果直接登记成 pending，下一轮会把整版帖子一次
        刷上颜色（几百条管理记录）；所以这里只登记，想真上色再用 --highlight-legacy
        限量提升。标题带删除线（已删除/测试稿/撤掉 AI 翻译的旧帖）和落在回收区等
        排除版块里的帖子一律不登记。
        """
        stats = {"threads": 0, "matched": 0, "recorded": 0, "skipped": 0, "refreshed": 0,
                 "no_color": 0, "recycled": 0, "excluded": 0, "site_bold": 0, "expired": 0,
                 "pruned": 0}
        threads = self.fetch_my_threads(pages=pages)
        stats["threads"] = len(threads)
        if not threads:
            return stats

        records_path = thread_records_path(save_dir)
        records = load_thread_records(records_path)
        titles = _scan_output_titles(save_dir)
        colors = self.highlight.get("colors") or {}
        exclude = self.highlight.get("exclude_forums") or []
        # 颜色、加粗、发帖日期只有版块列表看得到：抓一次，登记和过期判断都用它
        try:
            site_rows = {row.tid: row for row in self.fetch_forum_threads(pages=pages)}
        except requests.RequestException as e:
            log_warning("    ⚠ 读版块列表失败，历史登记的颜色/加粗回退到配置值: %s", e)
            site_rows = {}

        for item in threads:
            stem = titles.get(_normalize_title(item.title))
            if not stem:
                continue
            stats["matched"] += 1
            site_row = site_rows.get(item.tid)
            if self._stale_thread(site_row, _artifact_release_time(save_dir, stem)):
                stats["expired"] += 1
                log_debug("    → 跳过历史登记（已超过 %d 天）: %s",
                          self._max_age_days(), stem)
                continue
            if item.recy:
                stats["recycled"] += 1
                log_info("    → 跳过历史登记（标题带删除线）: %s", item.title)
                # 已有的记录也要作废：帖子带删除线 = 已删/测试稿/撤掉 AI 翻译，
                # 留着记录的话下一次 --highlight-legacy 还会把它重新刷上高亮
                if stem in records and records[stem].get("state") == "legacy":
                    records.pop(stem, None)
                    stats["pruned"] += 1
                continue
            skipped_reason = _forum_excluded(item.forum_name, item.forum_fid, exclude)
            if skipped_reason:
                stats["excluded"] += 1
                log_info("    → 跳过历史登记（%s）: %s", skipped_reason, item.title)
                continue
            if stem in records:
                # 历史登记是可刷新的：早先版本把 bold 写死成 false，重跑要能纠正
                stats["skipped"] += 1
                existing = records[stem]
                if existing.get("state") == "legacy" and self._refresh_legacy(
                        existing, site_row, colors):
                    stats["refreshed"] += 1
                continue

            news_type = _local_news_type(save_dir, stem)
            site_color = site_row.color if site_row else ""
            color = site_color or colors.get(news_type, "")
            if not color:
                stats["no_color"] += 1
                log_debug("    → 跳过历史登记（类型没有高亮色）: %s（%s）", stem, news_type)
                continue
            records[stem] = {
                "tid": item.tid,
                "fid": item.forum_fid or self.forum_fid,
                "typeid": (site_row.typeid if site_row else None) or self.sortid_map.get(news_type),
                "url": f"{self.base_url}/thread-{item.tid}-1-1.html",
                "title": item.title,
                "forum_name": item.forum_name,
                "news_type": news_type,
                "color": color,
                "bold": bool(site_row.bold) if site_row else False,
                "state": "legacy",
                # 发帖日期取版块列表上的真实值：留空会让 _record_time 退到登记时间，
                # 一篇两个月前的帖子就被当成今天发的，老到该扔还留在窗口里
                "posted_at": (f"{site_row.posted}T00:00:00"
                              if site_row and site_row.posted else ""),
                "registered_at": _timestamp(),
                "updated_at": _timestamp(),
                "attempts": 0,
                "last_error": "",
                "tid_source": "历史登记",
            }
            stats["recorded"] += 1
            if site_row and site_row.bold:
                stats["site_bold"] += 1
            if dry_run:
                log_info("    · 将登记: %s | tid=%s | %s %s%s | %s",
                         stem, item.tid, news_type, color,
                         "（站点已加粗）" if site_row and site_row.bold else "",
                         item.forum_name or "版块未知")

        if not dry_run:
            stats["pruned"] += self._drop_stale_legacy(records, save_dir, site_rows)
        if (stats["recorded"] or stats["refreshed"] or stats["pruned"]) and not dry_run:
            self._save_records(records_path, records)
        for name, fid in sorted({(item.forum_name, item.forum_fid)
                                 for item in threads if item.forum_name}):
            log_debug("    · 列表出现的版块: %s（fid=%s）", name, fid)
        log_info("[历史] 列表 %d 条 | 对上产物 %d 条 | 新登记 %d 条（其中站点已加粗 %d 条）| "
                 "已有记录 %d 条（刷新 %d 条）| 删除线跳过 %d 条 | 排除版块跳过 %d 条 | "
                 "无色跳过 %d 条 | 过期跳过 %d 条 | 清理过期登记 %d 条",
                 stats["threads"], stats["matched"], stats["recorded"], stats["site_bold"],
                 stats["skipped"], stats["refreshed"],
                 stats["recycled"], stats["excluded"], stats["no_color"], stats["expired"],
                 stats["pruned"])
        return stats

    def highlight_status(self, records_path: str, pages: int = 3) -> dict:
        """对照站点现状检查每篇记录的颜色与加粗（只读，不改论坛）。

        高亮样式只渲染在版块列表里，所以「到底生效了没有」只能拿列表来比：
        这里逐条打印「记录 vs 站点」，想验证某次操作是否成功直接看这个。
        """
        records = load_thread_records(records_path)
        stats = {"checked": 0, "same": 0, "different": 0, "missing": 0}
        if not records:
            log_info("[状态] 还没有帖子记录（先发一篇或跑 --index-history）")
            return stats

        try:
            site_rows = {row.tid: row for row in self.fetch_forum_threads(pages=pages)}
        except requests.RequestException as e:
            log_error("[状态] 读版块列表失败: %s", e)
            return stats

        for stem, rec in sorted(records.items()):
            tid = rec.get("tid")
            if not tid:
                continue
            stats["checked"] += 1
            row = site_rows.get(int(tid))
            if row is None:
                stats["missing"] += 1
                log_info("  ? %s | tid=%s | 前 %d 页里没有（未过审/已删除/更靠后）",
                         rec.get("title") or stem, tid, pages)
                continue
            site_color = row.color or "无颜色"
            site_bold = "加粗" if row.bold else "不加粗"
            rec_color = rec.get("color") or "无颜色"
            rec_bold = "加粗" if rec.get("bold") else "不加粗"
            same = site_color.upper() == rec_color.upper() and site_bold == rec_bold
            if same:
                stats["same"] += 1
            else:
                stats["different"] += 1
            log_info("  %s | tid=%s | 记录 %s %s | 站点 %s %s | %s",
                     "一致" if same else "不一致", tid, rec_color, rec_bold,
                     site_color, site_bold, rec.get("title") or stem)
        log_info("[状态] 检查 %d 条 | 一致 %d | 不一致 %d | 没找到 %d",
                 stats["checked"], stats["same"], stats["different"], stats["missing"])
        return stats

    def _refresh_legacy(self, record: dict, site_row, colors: dict) -> bool:
        """用版块列表的现状刷新一条 legacy 记录，返回是否有变化。

        历史登记只写本地记录、不上色，站点上的颜色/加粗随时可能被人改过；重跑
        --index-history 时以站点为准纠正，避免记录和站上长期不一致。分类和发帖日期
        一起补：早先版本没写这两个字段，缺了分类就没法参与「同组只留最新一篇加粗」。
        """
        bold = bool(site_row.bold) if site_row else bool(record.get("bold"))
        color = (site_row.color if site_row else "") or record.get("color", "")
        if not color:
            color = colors.get(record.get("news_type", ""), "")
        typeid = record.get("typeid")
        if not typeid:
            typeid = (getattr(site_row, "typeid", None) if site_row else None) or \
                self.sortid_map.get(record.get("news_type", ""))
        posted = record.get("posted_at") or ""
        if site_row is not None and getattr(site_row, "posted", ""):
            posted = f"{site_row.posted}T00:00:00"
        changed = (bold != bool(record.get("bold"))) or (color != record.get("color")) \
            or (typeid != record.get("typeid")) or (posted != (record.get("posted_at") or ""))
        if not changed:
            return False
        record["bold"] = bold
        record["color"] = color
        record["typeid"] = typeid
        if posted:
            record["posted_at"] = posted
        record["updated_at"] = _timestamp()
        return True

    def promote_legacy(self, records_path: str, limit: int) -> int:
        """把最近的 N 条历史登记提升成待补高亮；真正的上色交给补高亮流程。"""
        records = load_thread_records(records_path)
        legacy = [(stem, rec) for stem, rec in records.items() if rec.get("state") == "legacy"]
        legacy.sort(key=lambda item: int(item[1].get("tid") or 0), reverse=True)

        promoted = 0
        for stem, rec in legacy[:max(limit, 0)]:
            if not rec.get("tid") or not rec.get("color"):
                log_warning("    ⚠ 跳过历史登记（缺帖子 ID 或色值）: %s", stem)
                continue
            rec["state"] = "pending"
            rec["attempts"] = 0
            rec["last_error"] = ""
            # 提升等于「从现在开始等审核」：registered_at 不重置的话，一篇老帖的提升
            # 会让 _expired 立刻判超时，等于白提升
            rec["registered_at"] = _timestamp()
            rec["updated_at"] = _timestamp()
            promoted += 1
            log_info("    → 提升为待高亮: %s（tid=%s）", stem, rec.get("tid"))
        self._save_records(records_path, records)
        return promoted

    def _register_highlight(self, stem: str, news_dir: str, result: PostResult,
                            news_type: str, title: str) -> None:
        """登记这篇帖子的高亮意图，发帖时不动手。

        站点对新主题一律走审核，而审核中的帖子在 Discuz 里设置不了高亮，所以统一
        先落一条 pending 记录，交给每轮的补高亮流程（匿名探测过审 → 登录 → 设置）。
        """
        if not self.highlight.get("enabled"):
            return
        color = (self.highlight.get("colors") or {}).get(news_type, "")
        if not color:
            log_debug("    → 类型 %s 没有配置高亮色，跳过登记", news_type)
            return
        if not highlight_color_index(color):
            # 配置写了个不在 Discuz 色板里的色值：登记了也一定会被站点拒绝，
            # 不如当场报出来（别让它变成一轮又一轮的重试）
            log_warning("    ⚠ 类型 %s 的色值 %s 不在 Discuz 色板内，跳过高亮登记",
                        news_type, color)
            return

        path = thread_records_path(news_dir)
        records = load_thread_records(path)
        previous = records.get(stem) or {}
        now = _timestamp()
        # 同一个 stem 又发了一次（重试或重发）：旧记录的 tid 和年龄属于上一篇帖子，
        # 沿用会被当成「还是那一篇」——高亮打到旧帖，或按老年龄直接判过期
        same_thread = bool(result.tid) and result.tid == previous.get("tid")
        posted_at = previous.get("posted_at") if same_thread else ""
        records[stem] = {
            "tid": result.tid,
            "fid": self.forum_fid,
            "typeid": self.sortid_map.get(news_type),
            "url": (result.url if result.url.startswith("http")
                    else (previous.get("url", "") if same_thread else "")),
            "title": title,
            "news_type": news_type,
            "color": color,
            "bold": news_type in (self.highlight.get("bold_types") or []),
            "state": "pending",
            "posted_at": posted_at or now,
            "registered_at": now,
            "updated_at": now,
            "attempts": 0,
            "last_error": "",
        }
        # 走 _save_records 而不是 save_thread_records：它会先按窗口清理过期记录，
        # 否则「只发帖、没别的写盘」的运行永远不清账本
        before = _records_digest(records)
        self._save_records(path, records)
        if _records_digest(records) != before:
            log_debug("    → 高亮登记时顺带清理了过期记录")

        tid_hint = f"tid={result.tid}" if result.tid else "帖子 ID 待从列表补齐"
        bold_hint = "（加粗）" if records[stem]["bold"] else ""
        log_info(f"    → 已登记待补高亮: {news_type} {color}{bold_hint}，{tid_hint}")

    def post_news_file(self, stem: str, txt_path: str, json_path: str,
                       news_dir: str, no_image: bool = False,
                       attach_json: bool = True) -> str:
        """
        发布单个新闻文件（txt + json + 可选图片 + 可选 JSON 附件）

        Args:
            attach_json: 是否将 JSON 文件作为附件上传并插入下载链接

        Returns:
            帖子 URL
        """
        # 加载元数据
        with open(json_path, encoding="utf-8") as f:
            meta = json.load(f)
        title = meta.get("translated_title", "").strip()
        if not title:
            title = meta.get("title", "").strip()
        if not title:
            raise ValueError(f"{json_path} 中找不到 title 字段")
        title = title + " [AI翻译]"

        with open(txt_path, encoding="utf-8") as f:
            message = f.read().strip()
        if not message:
            raise ValueError(f"{txt_path} 内容为空")

        # 上传图片
        attachment_ids = []
        if not no_image:
            img_path = find_image(news_dir, stem)
            if img_path:
                try:
                    aid = self.upload_image(img_path)
                    message = insert_image_bbcode(message, aid)
                    attachment_ids.append(aid)
                except Exception as e:
                    log_warning(f"    ⚠ 图片上传失败，继续无图发帖: {e}")

        # 上传 JSON 附件并追加到正文末尾
        if attach_json and os.path.exists(json_path):
            try:
                json_aid = self.upload_file(json_path, mime_type="text/plain")  # Discuz 不接受 application/json
                message += f"\n\n[attach]{json_aid}[/attach]"
                attachment_ids.append(json_aid)
            except Exception as e:
                log_warning(f"    ⚠ JSON 附件上传失败，跳过: {e}")

        # 检测分类（同时检测英文和中文标题，避免关键词丢失）
        original_title = meta.get("title", "").strip()
        module_type = _resolve_news_type(message, original_title, title)
        sortid = self.sortid_map.get(module_type)
        if sortid:
            cat_name = _CATEGORY_GROUP.get(module_type, "未知")
            log_info(f"    分类: {cat_name} (sortid={sortid})")
        else:
            log_warning(f"    ⚠ 警告: 未找到分类 {module_type} 的 sortid 配置")

        result = self.post_thread(title, message,
                                  attachment_ids=attachment_ids, sortid=sortid)
        self._register_highlight(stem, news_dir, result, module_type, title)
        return result.url


# ── 状态管理 ─────────────────────────────────────────
# load_posted / save_posted 已上移到 utils（B站 侧也要用），本模块从 utils 导入，
# `from poster import load_posted` 的老调用方与测试仍然有效。


def _login_config_hint(config: dict) -> str:
    """账号密码缺失时的排查提示：只说有没有，不输出任何值。"""
    missing = [name for name, key in (("MCBBS_USERNAME", "username"),
                                      ("MCBBS_PASSWORD", "password"))
               if not config.get(key)]
    env_path = os.path.join(PROJECT_ROOT, ".env")
    env_loaded = bool(os.environ.get("MCBBS_USERNAME") or os.environ.get("MCBBS_PASSWORD"))
    return (
        f"缺 {' / '.join(missing) or '（无）'}；.env={env_path}"
        f"（{'存在' if os.path.isfile(env_path) else '不存在'}），"
        f"环境变量{'已读到' if env_loaded else '未读到'}；"
        f"也可以写进 config/config.json 的 mcbbs.username / mcbbs.password"
    )


# ── CLI 入口 ─────────────────────────────────────────

def _preview_records(records_path: str, state: str, limit: int | None = None) -> int:
    """打印指定状态的帖子记录（--dry-run 用），返回条数。"""
    records = [rec for rec in load_thread_records(records_path).values()
               if rec.get("state") == state]
    records.sort(key=lambda rec: int(rec.get("tid") or 0), reverse=True)
    if limit is not None:
        records = records[:max(limit, 0)]
    for rec in records:
        color = rec.get("color") or ""
        index = highlight_color_index(color)
        color_hint = f"{color}（色号 {index}）" if index else (color or "无颜色")
        log_info("  tid=%s | %s | %s | %s | %s", rec.get("tid"), color_hint,
                 "加粗" if rec.get("bold") else "不加粗", state, rec.get("title", ""))
    log_info("[Dry Run] state=%s 共 %d 条", state, len(records))
    return len(records)


def _targeted_exit_code(stats: dict) -> int:
    """单篇高亮命令的退出码：只有「真的设上了」才算成功。

    `failed` 只统计站点拒绝/请求出错那两类；记录过期（stale）、等审核超时（gave_up）、
    落在排除版块（dropped）都不加 `failed`，只判它会让没生效的单篇验证也以 0 退出，
    在脚本里等于谎报成功。
    """
    applied = int(stats.get("highlighted") or 0)
    return 0 if applied and not stats.get("failed") else 1


def _run_highlight_cli(args, config: dict, news_dir: str, records_path: str) -> int:
    """--highlight-only / --index-history / --highlight-legacy 的入口，都不发帖。"""
    if not os.path.isdir(news_dir):
        log_error(f"[!] 新闻目录不存在: {news_dir}")
        return 1

    if args.index_history and (not config.get("username") or not config.get("password")):
        log_error("[!] 未配置 MCBBS 账号密码！%s", _login_config_hint(config))
        return 1

    poster = MCBBSPoster(config)

    if args.highlight_status:
        stats = poster.highlight_status(records_path)
        # 读不到版块列表时 highlight_status 什么都没查（checked=0），别报成功
        return 0 if stats.get("checked") else 1

    if args.reconcile_bold:
        try:
            stats = poster.reconcile_bold(records_path, dry_run=args.dry_run)
        except Exception as e:
            log_error(f"[!] 加粗收敛失败: {e}")
            return 1
        return 0 if stats.get("ok") else 1

    if args.highlight_stem:
        records = load_thread_records(records_path)
        record = records.get(args.highlight_stem)
        if record is None:
            log_error("[!] 记录里没有这篇：%s", args.highlight_stem)
            return 1
        if args.bold:
            record["bold"] = True
        elif args.no_bold:
            record["bold"] = False
        if args.dry_run:
            color = record.get("color") or ""
            log_info("[Dry Run] 将提交: tid=%s | %s（色号 %s）| %s | %s",
                     record.get("tid"), color, highlight_color_index(color),
                     "加粗" if record.get("bold") else "不加粗", record.get("title", ""))
            return 0
        record["state"] = "pending"
        record["attempts"] = 0
        record["last_error"] = ""
        record["registered_at"] = _timestamp()
        record["updated_at"] = _timestamp()
        poster._save_records(records_path, records)
        log_info("[高亮] 已把 %s 标记为待补高亮（tid=%s）",
                 args.highlight_stem, record.get("tid"))
        # 只处理这一条：别的 pending 是别的运行该管的，别被单篇验证顺手带上
        try:
            stats = poster.process_pending_highlights(records_path, only=args.highlight_stem)
        except Exception as e:
            log_error(f"[!] 补高亮失败: {e}")
            return 1
        return _targeted_exit_code(stats)

    if args.highlight_tid:
        if not highlight_color_index(args.color or ""):
            log_error("[!] --highlight-tid 需要用 --color 指定 Discuz 色板内的色值"
                      "（顶栏色板：#EE1B2E / #EE5023 / #996600 / #3C9D40 / #2897C5 / "
                      "#2B65B7 / #8F2A90 / #EC1282），收到: %s", args.color or "未指定")
            return 1
        records = load_thread_records(records_path)
        # 站点上有同 tid 的记录就复用它，别再另建一条 tid_<n>：同一篇帖子出现两条记录
        # 会让 --highlight-status 报两遍、_record_for_tid 也可能读到过期的那条
        key = next((stem for stem, rec in records.items()
                    if rec.get("tid") == args.highlight_tid), f"tid_{args.highlight_tid}")
        record = dict(records.get(key) or {})
        try:
            row = next((item for item in poster.fetch_forum_threads(pages=3)
                        if item.tid == args.highlight_tid), None)
        except requests.RequestException as e:
            log_error("[!] 读版块列表失败，无法确认这篇帖子的现状: %s", e)
            return 1
        if row is None:
            log_warning("    ⚠ 版块列表前 3 页里没有 tid=%s，不参与「同组只留最新一篇加粗」",
                        args.highlight_tid)
        if args.bold:
            bold = True
        elif args.no_bold:
            bold = False
        else:
            # 没显式指定就沿用站上现状：默认 False 会把人家人工加粗的帖子取消加粗
            bold = bool(row.bold) if row is not None else bool(record.get("bold"))
        record.update({
            "tid": args.highlight_tid,
            "fid": poster.forum_fid,
            # 查不到这一行就别留着旧分类：过期的 typeid 会让收敛去动别的帖子
            "typeid": row.typeid if row else None,
            "url": f"{poster.base_url}/thread-{args.highlight_tid}-1-1.html",
            "title": row.title if row else record.get("title", ""),
            "news_type": record.get("news_type", "manual"),
            "color": args.color,
            "bold": bold,
            "state": "pending",
            # 发帖日期用版块列表上的真实值，否则 _too_old 会把一篇老帖当成今天发的
            "posted_at": (f"{row.posted}T00:00:00"
                          if row and row.posted else (record.get("posted_at") or _timestamp())),
            "registered_at": _timestamp(),
            "updated_at": _timestamp(),
            "attempts": 0,
            "last_error": "",
            "tid_source": "手工指定",
        })
        if args.dry_run:
            log_info("[Dry Run] 将提交: tid=%s | %s（色号 %s）| %s | %s",
                     args.highlight_tid, args.color, highlight_color_index(args.color),
                     "加粗" if record["bold"] else "不加粗", record["title"] or "标题未知")
            return 0
        records[key] = record
        poster._save_records(records_path, records)
        log_info("[高亮] 已登记手工指定的帖子: tid=%s（%s %s）",
                 args.highlight_tid, args.color,
                 "加粗" if record["bold"] else "不加粗")
        try:
            stats = poster.process_pending_highlights(records_path, only=key)
        except Exception as e:
            log_error(f"[!] 补高亮失败: {e}")
            return 1
        return _targeted_exit_code(stats)

    if args.index_history:
        try:
            poster.login()
        except Exception as e:
            log_error(f"[!] 登录失败: {e}")
            return 1
        poster.index_history(news_dir, dry_run=args.dry_run)
        if args.dry_run:
            log_info("[Dry Run] 以上是历史登记结果，未写入任何记录")
        return 0

    if args.highlight_legacy is not None:
        if args.dry_run:
            _preview_records(records_path, "legacy", args.highlight_legacy)
            return 0
        promoted = poster.promote_legacy(records_path, args.highlight_legacy)
        log_info("[高亮] 已提升 %d 条历史登记为待补高亮", promoted)

    if args.dry_run:
        _preview_records(records_path, "pending")
        log_info("[Dry Run] 以上是待补高亮队列，未做任何论坛操作")
        return 0

    try:
        poster.process_pending_highlights(records_path)
    except Exception as e:
        log_error(f"[!] 补高亮失败: {e}")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="MCBBS 新闻自动发帖（含图片上传）")
    parser.add_argument("files", nargs="*", help="指定要发布的文件（stem）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--dir", default="./output", help="新闻目录")
    parser.add_argument("--no-image", action="store_true", help="跳过图片上传")
    parser.add_argument("--no-json", action="store_true", help="跳过 JSON 附件上传")
    parser.add_argument("--fid", type=int, help="覆盖版块 ID")
    parser.add_argument("--highlight-only", action="store_true",
                        help="只处理待补高亮（审核已通过的帖子），不发帖")
    parser.add_argument("--index-history", action="store_true",
                        help="把「我的帖子」里对得上 output 产物的历史帖登记为 legacy（只写本地记录）")
    parser.add_argument("--highlight-legacy", type=int, metavar="N",
                        help="把最近 N 条历史登记提升为待高亮并执行（会真的改论坛，先用 --dry-run 看）")
    parser.add_argument("--highlight-status", action="store_true",
                        help="对照站点现有颜色/加粗检查本地记录（只读，不改论坛）")
    parser.add_argument("--reconcile-bold", action="store_true",
                        help="按分类+版本线（正式版/测试版）各自只留最新一篇加粗（会真的改论坛，"
                             "先配 --dry-run 看）")
    parser.add_argument("--highlight-stem", metavar="STEM",
                        help="只补这一篇的高亮（单篇验证用，配合 --bold/--no-bold 可强制样式）")
    parser.add_argument("--highlight-tid", type=int, metavar="TID",
                        help="不依赖 output 产物，直接对指定帖子设置高亮（需配 --color；"
                             "不加 --bold/--no-bold 时沿用站上现状）")
    parser.add_argument("--color", metavar="HEX",
                        help="配合 --highlight-tid：Discuz 色板内的色值，如 #EC1282")
    parser.add_argument("--bold", action="store_true",
                        help="配合 --highlight-stem / --highlight-tid：这次强制加粗")
    parser.add_argument("--no-bold", action="store_true",
                        help="配合 --highlight-stem / --highlight-tid：这次强制不加粗")
    args = parser.parse_args()

    config = load_poster_config()
    if args.fid is not None:
        config["forum_fid"] = args.fid

    news_dir = args.dir
    state_file = os.path.join(news_dir, ".posted.json")

    if (args.highlight_only or args.index_history or args.highlight_status
            or args.reconcile_bold
            or args.highlight_stem or args.highlight_tid
            or args.highlight_legacy is not None):
        sys.exit(_run_highlight_cli(args, config, news_dir, thread_records_path(news_dir)))

    if not config["username"] or not config["password"]:
        log_error("[!] 未配置 MCBBS 账号密码！%s", _login_config_hint(config))
        sys.exit(1)

    # 扫描新闻文件
    if not os.path.isdir(news_dir):
        log_error(f"[!] 新闻目录不存在: {news_dir}")
        sys.exit(1)

    all_news = []
    for txt_path in sorted(glob.glob(os.path.join(news_dir, "*.txt"))):
        stem = os.path.splitext(os.path.basename(txt_path))[0]
        json_path = os.path.join(news_dir, stem + ".json")
        if os.path.exists(json_path):
            all_news.append((stem, txt_path, json_path))
        else:
            log_warning(f"    ⚠ 跳过 {stem}: 找不到 .json")

    if not all_news:
        log_info("[!] 没有找到新闻文件")
        sys.exit(0)

    posted = load_posted(state_file)
    if args.files:
        pending = [(s, t, j) for s, t, j in all_news if s in set(args.files)]
    else:
        pending = [(s, t, j) for s, t, j in all_news if s not in posted]

    if not pending:
        log_info("[!] 没有需要发布的新闻")
        sys.exit(0)

    log_info(f"[*] 待发布: {len(pending)} 个")

    if args.dry_run:
        for stem, txt_path, json_path in pending:
            with open(json_path, encoding="utf-8") as f:
                meta = json.load(f)
            title = meta.get("title") or meta.get("translated_title", "")
            with open(txt_path, encoding="utf-8") as f:
                message = f.read().strip()
            img_path = find_image(news_dir, stem)
            module_type = detect_module_type(message, title)
            cat = _CATEGORY_GROUP.get(module_type, "未分类") if module_type else "未分类"
            log_info(f"\n  文件: {stem}")
            log_info(f"  标题: {title}")
            log_info(f"  分类: {cat}")
            log_info(f"  图片: {'✓ ' + os.path.basename(img_path) if img_path else '✗ 无'}")
        log_info(f"\n[Dry Run] 共 {len(pending)} 个待发布")
        sys.exit(0)

    poster = MCBBSPoster(config)
    try:
        poster.login()
    except Exception as e:
        log_error(f"[!] 登录失败: {e}")
        sys.exit(1)

    success = 0
    failed = 0
    for stem, txt_path, json_path in pending:
        try:
            log_info(f"\n[*] 发布: {stem}")
            poster.post_news_file(stem, txt_path, json_path, news_dir,
                                 no_image=args.no_image, attach_json=not args.no_json)
            posted.add(stem)
            save_posted(state_file, posted)
            success += 1
            time.sleep(2)
        except Exception as e:
            log_error(f"    ✗ 发布失败: {e}")
            failed += 1

    log_info(f"\n{'=' * 50}")
    log_info(f"  完成！成功: {success}, 失败: {failed}")
    log_info(f"{'=' * 50}")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    # .env 里的 MCBBS_USERNAME / MCBBS_PASSWORD 只有显式加载一次才会进环境变量；
    # main.py 在 import 时就做了，这条独立入口（--highlight-only 等）以前漏了
    load_dotenv()
    setup_logging()
    main()
